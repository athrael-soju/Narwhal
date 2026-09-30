---
description: Freeze the deployment inputs and discover the hosts of a Narwhal vLLM fleet.
---

# Gate A: Freeze inputs and discover the real deployment

## Load the private environment

1. In a fresh management checkout, copy `.env.example` to `.env`.
2. Restrict `.env` to mode 0600.
3. Fill every field.
4. Load `.env` with shell tracing disabled:

    ```bash
    set +x
    set -a
    . ./.env
    set +a
    ```

Set the SSH destinations:

| Variable               | Host                               | Default                                  |
| ---------------------- | ---------------------------------- | ---------------------------------------- |
| `NARWHAL_NODE_<n>_SSH` | Engine `<n>`, one entry per engine |                                          |
| `NARWHAL_ROUTER_SSH`   | Router                             | The lowest-numbered engine's destination |

Discovery requires at least two engines.

A destination is `user@host` or an OpenSSH alias carrying the username, port, identity, and jump host.

Roles that share a destination share one host entry and credential.

| Authentication | Credential                                                                                |
| -------------- | ----------------------------------------------------------------------------------------- |
| Password       | The destination variable with a `_PASSWORD` suffix, such as `NARWHAL_NODE_1_SSH_PASSWORD` |
| Key            | The configured identity or the SSH agent                                                  |

Default service URLs use the global address on `NARWHAL_FABRIC_INTERFACE`:

| Service     | Default URL                                             |
| ----------- | ------------------------------------------------------- |
| Engine      | `http://<address>:<NARWHAL_ENGINE_PORT>`                |
| Attestation | `http://<address>:<NARWHAL_ATTEST_PORT>/v1/attestation` |

Per-node overrides:

| Override                                                       | Use when                                                      |
| -------------------------------------------------------------- | ------------------------------------------------------------- |
| `NARWHAL_NODE_<n>_IP`                                          | The interface has several global addresses.                   |
| `NARWHAL_NODE_<n>_URL`                                         | The engine service is reachable through another address.      |
| `NARWHAL_NODE_<n>_ATTESTATION_URL`                             | The attestation service is reachable through another address. |
| `NARWHAL_NODE_<n>_ENGINE_PORT`, `NARWHAL_NODE_<n>_ATTEST_PORT` | The service is bound to a different port.                     |

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

4. Record the repository ID and commit SHA in the private record.

Discovery compares the model directories across replicas:

| Model directory content                             | Replica comparison      |
| --------------------------------------------------- | ----------------------- |
| Every regular file                                  | Path, size, and SHA-256 |
| Root `README.md` and `.cache/huggingface/` metadata | Excluded                |

A replica mismatch stops discovery with an error that names the first differing file.

## Run discovery and access checks

Each engine host needs:

- Python 3
- Docker
- `ip`
- `rocminfo` or `nvidia-smi`
- the pinned engine image
- the checkpoint at `NARWHAL_MODEL_DIR`
- the run directory at `NARWHAL_RUN_DIR`

From the management checkout:

```bash
python3 tools/deployment/discover_deployment.py --out runs/discovery/first-deploy
. config/deployment.env
python3 tools/deployment/deploy_hosts.py plan
python3 tools/deployment/deploy_hosts.py check-access
```

| Command        | Result                                                                                                        |
| -------------- | ------------------------------------------------------------------------------------------------------------- |
| Discovery      | Prints `Generated private configuration. Load config/deployment.env before preparation.`                      |
| `plan`         | Prints each host ID and its roles.                                                                            |
| `check-access` | Logs in once per physical host with the pinned key and saves the `hostname` output under `runs/access-<id>/`. |

Open a login shell on a role's host:

```bash
python3 tools/deployment/deploy_hosts.py shell --role engine-1
```

`--role` accepts `router` or `engine-<n>`.

Host-key handling:

| Setting                   | Value                                                       |
| ------------------------- | ----------------------------------------------------------- |
| Store                     | `NARWHAL_SSH_KNOWN_HOSTS`, default `config/ssh.known_hosts` |
| Discovery                 | Records each new host key and matches stored keys.          |
| Other deployment commands | Match every host key against the store.                     |

### Discovery output

Launch settings derived from the checkpoint:

| Checkpoint condition                                   | Setting                         |
| ------------------------------------------------------ | ------------------------------- |
| `auto_map` in `config.json` or `tokenizer_config.json` | `--trust-remote-code`           |
| Convolutional SSM transfer state                       | `VLLM_SSM_CONV_STATE_LAYOUT=DS` |

Discovery detects convolutional SSM transfer state from any of these fields in `text_config`, or in the root configuration otherwise:

- `linear_attn_config.kda_layers` with `linear_attn_config.short_conv_kernel_size`
- `mamba_d_conv` or `mamba_d_state`
- `linear_conv_kernel_dim` with a `linear_attention` entry in `layer_types`
- a `layer_types` entry containing `mamba` or `ssm`

Generated configuration, mode 0600:

| File                                | Content                                                                                                            |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `config/hosts.local.json`           | Host IDs, SSH destination and password variable names, and the router and engine roles on each host                |
| `config/ssh.known_hosts`            | Server public keys from authenticated management access                                                            |
| `config/engine-launch.local.json`   | GPU allocation, image and model metadata, transfer devices, launch policy                                          |
| `config/engine-launch.sources.json` | Inspection and policy records for each role                                                                        |
| `config/fleet.json`                 | Model, measured hardware and TP shape, engine URL references, initial roles, initial latency targets, profile path |
| `config/deployment.env`             | Generated paths, fabric addresses, engine and attestation URLs, per-engine image and hash values                   |

Keep the generated `config/` files together.

The discovery output directory, mode 0700:

| File                                                         | Content                                                                                                                                                                                              |
| ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `engine-<n>.json`                                            | GPU products and device mappings, image identity and package versions, model-config hash and dtype, custom-code and convolutional-state flags, fabric interface addresses, image runtime environment |
| `engine-<n>-checkpoint.json`                                 | Path, size, and SHA-256 of every checkpoint file, the exclusion policy, and `model_tree_sha256`                                                                                                      |
| `logs/`                                                      | SSH command logs                                                                                                                                                                                     |
| `engine-<n>-inputs.json`, `engine-launch.json`, `hosts.json` | Probe inputs and the candidate launch and host records                                                                                                                                               |
| `manifest.json`                                              | Hashes of the generated `config/` files, the engine roles, and the shared `model_tree_sha256`                                                                                                        |

Reuse an inspected fleet:

1. Reload `.env` and `config/deployment.env`.
2. Verify access.
3. Prepare a new deployment run.

Rerun discovery after a change to the hardware, model, image, checkpoint, launch policy, route, transport, or another discovery input:

1. Archive the generated `config/` files.
2. Run discovery into a new output directory.

## Confirm the launch policy

| Engine roles on a GPU host | GPU allocation                                                |
| -------------------------- | ------------------------------------------------------------- |
| One                        | Every detected GPU, with tensor parallelism set to that count |
| Several                    | Declare disjoint `NARWHAL_NODE_<n>_GPU_IDS` lists             |

Every replica must have the same accelerator product, GPU count, and TP size.

Default serving policy:

| Setting                | Default                                                        |
| ---------------------- | -------------------------------------------------------------- |
| Transfer               | TCP on `NARWHAL_FABRIC_INTERFACE`                              |
| Model dtype            | The checkpoint's dtype, otherwise bfloat16                     |
| KV dtype               | automatic                                                      |
| Requested cache block  | 128 tokens                                                     |
| Execution              | eager                                                          |
| Maximum context        | The model's `max_position_embeddings`, capped at 16,384 tokens |
| Maximum sequences      | 8                                                              |
| GPU memory utilisation | 0.9                                                            |
| Initial TTFT limit     | 10 s                                                           |
| Initial TPOT limit     | 0.125 s                                                        |
| Initial prefill pool   | The lowest-numbered engine                                     |
| Initial decode pool    | The remaining engines                                          |
| Profile path           | `runs/profiles.json` in the installed checkout                 |

Override the policy in `.env` before discovery:

| Field                                                                                    | Meaning                                                                                                                        |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `NARWHAL_GPU_IDS`, `NARWHAL_TENSOR_PARALLEL_SIZE`                                        | Comma-separated GPU indices or NVIDIA UUIDs, and the replica TP size.                                                          |
| `NARWHAL_MODEL_DTYPE`, `NARWHAL_BLOCK_SIZE`                                              | Model dtype (`bfloat16` or `float16`) and requested cache block size.                                                          |
| `NARWHAL_ENGINE_ARGS`                                                                    | JSON array that replaces the default vLLM arguments and holds one `--max-num-seqs` value.                                      |
| `NARWHAL_ENGINE_ENV`                                                                     | JSON object of launcher-supported environment fields that overrides the image environment.                                     |
| `NARWHAL_TRANSFER_TRANSPORT`, `NARWHAL_TRANSFER_NET_DEVICES`, `NARWHAL_TRANSFER_DEVICES` | Transport (`ucx_tcp` or `ucx_rdma`), UCX network devices (`HCA:port` entries for RDMA), and a JSON array of RDMA device paths. |
| `NARWHAL_TTFT_S`, `NARWHAL_TPOT_S`                                                       | Initial TTFT and TPOT limits, in seconds.                                                                                      |
| `NARWHAL_ENGINE_KEY`                                                                     | Engine API key that discovery names in `engine.engine_api_key_env`.                                                            |

Per-engine overrides use `NARWHAL_NODE_<n>_<field>`, such as `NARWHAL_NODE_2_GPU_IDS`.

## Access failure handling

A failed deployment command prints `<host-id>: blocked at <operation>; inspect its private command log`.

| Symptom                              | Fix                                                                                                                                                  |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Missing access variable              | Set the named `.env` field.                                                                                                                          |
| New or changed host key              | 1. Verify the destination and fingerprint independently, such as through the provider console.<br>2. Replace the entry in `NARWHAL_SSH_KNOWN_HOSTS`. |
| Authentication or connection problem | Check the username, credential, route, SSH port, and firewall.                                                                                       |

Record the first failing host and gate in the private record.

Continue with [Gate B: Package and install the approved revision](02-Install.md).
