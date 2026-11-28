---
description: Freeze the deployment inputs and discover the hosts of a Narwhal fleet.
---

# Discover the deployment

The first gate runs two stages. `access` logs in to every host. `discover` inspects each engine host's GPUs, engine image and checkpoint, and writes the host inventory, the launch records, the fleet and the derived variables that every later stage reads. Neither installs anything on a host.

Before the first `narwhal deploy run`, write the inputs file and the known-hosts file, and stage the checkpoint and the engine image on every engine host. Narwhal checks the checkpoint and the image but never copies them.

## Loading the private environment

`narwhal deploy` reads its inputs from the environment. Keep them in a private `.env` file at mode 0600, which [Deployment inputs](../configuration/04-Deployment-Inputs.md) lists in full, and load it with shell tracing disabled:

```bash
set +x
set -a
. ./.env
set +a
```

A source checkout holds `.env.example` with the common inputs.

Set one `NARWHAL_NODE_<n>_SSH` destination for each engine `<n>`. Discovery requires at least two engines with the same GPU product, GPU count and TP size. Engines may share a host: give each one its own `NARWHAL_NODE_<n>_GPU_IDS`. `NARWHAL_ROUTER_SSH` sets the router's destination and defaults to the lowest-numbered engine's destination.

A destination is `user@host` or an OpenSSH alias carrying the username, port, and identity. `narwhal deploy` ignores a jump host in your SSH config; set `NARWHAL_SSH_JUMP` instead, as in [Jump host](../configuration/04-Deployment-Inputs.md#jump-host). Roles that share a destination share one host entry and credential.

For password authentication, set the destination variable with a `_PASSWORD` suffix, such as `NARWHAL_NODE_1_SSH_PASSWORD`. Key authentication uses the configured identity or the SSH agent, and never prompts: load a key that has a passphrase into the agent first.

Set `NARWHAL_SSH_KNOWN_HOSTS` to a known-hosts file that holds the key of every destination and of the jump host. Collect each key from a source you trust, such as the provider console, and check its fingerprint before you add it. Every `narwhal deploy` command checks each host's key against this file before it logs in, and never adds or changes a key in it.

Default service URLs use the global address on `NARWHAL_FABRIC_INTERFACE`. The engine listens at `http://<address>:<NARWHAL_ENGINE_PORT>`, and the attestation service at `http://<address>:<NARWHAL_ATTEST_PORT>/v1/attestation`.

Per-node overrides:

| Override                                                       | Use when                                                     |
| -------------------------------------------------------------- | ------------------------------------------------------------ |
| `NARWHAL_NODE_<n>_IP`                                          | The interface has several global addresses                   |
| `NARWHAL_NODE_<n>_URL`                                         | The engine service is reachable through another address      |
| `NARWHAL_NODE_<n>_ATTESTATION_URL`                             | The attestation service is reachable through another address |
| `NARWHAL_NODE_<n>_ENGINE_PORT`, `NARWHAL_NODE_<n>_ATTEST_PORT` | The service is bound to a different port                     |

## Staging and identifying the checkpoint

Set `NARWHAL_ENGINE_MODEL_NAME` to the served model name and `NARWHAL_MODEL_DIR` to the checkpoint directory on each engine host.

For an empty directory with a Hugging Face source:

1. Set `MODEL_REPO_ID` to the repository.
2. Set `MODEL_REVISION` to the full commit SHA.
3. Stage the same snapshot on every engine host:

    ```bash
    hf download "$MODEL_REPO_ID" --revision "$MODEL_REVISION" --local-dir "$NARWHAL_MODEL_DIR"
    ```

4. Record the repository ID and commit SHA in the private record.
5. Compute the checkpoint's tree digest on one engine host. The digest module uses only the Python standard library, so the host runs it from the release wheel before anything is installed. With `.env` loaded on the workstation:

    ```bash
    scp "$NARWHAL_WHEEL" <host-a>:/tmp/narwhal.whl
    ssh <host-a> "echo '{\"model_dir\": \"$NARWHAL_MODEL_DIR\"}' |
      PYTHONPATH=/tmp/narwhal.whl python3 -m narwhal.deployment.launch_engine.checkpoint"
    ssh <host-a> rm /tmp/narwhal.whl
    ```

6. Set `NARWHAL_MODEL_TREE_SHA256` in `.env` to the output's `model_tree_sha256`.

Discovery hashes every regular file in each engine's model directory and refuses an engine whose tree digest differs from `NARWHAL_MODEL_TREE_SHA256`. It also compares the path, size, and SHA-256 of every file across replicas. The comparison excludes the root `README.md` and the `.cache/huggingface/` metadata. Discovery refuses a symbolic link that resolves outside the model directory, names an absolute path, or has a relative target that climbs above the directory, because the container mounts only that directory. A directory of links into a Hugging Face cache, such as a `snapshots/<revision>` directory, fails this check; stage the checkpoint with `hf download --local-dir` as above. A replica mismatch stops discovery with an error that names the first differing file. When the replicas match each other but not the expected digest, the error names the engine and the digest it found.

### Staging a GGUF model on vLLM

A vLLM fleet can serve a GGUF file from an engine image that includes `vllm-gguf-plugin`, such as the one in [Building a GGUF engine image](#building-a-gguf-engine-image). Stage the GGUF file in `NARWHAL_MODEL_DIR` beside the `config.json` and tokenizer files of the model it was converted from:

1. Set `BASE_REPO_ID` and `BASE_REVISION` to the repository and full commit SHA of the model the file was converted from.
2. Set `GGUF_REPO_ID`, `GGUF_REVISION` and `GGUF_FILE` to the GGUF repository, its full commit SHA and the file name.
3. Stage the same files on every engine host:

    ```bash
    hf download "$BASE_REPO_ID" --revision "$BASE_REVISION" \
      --include '*.json' --include '*.txt' --include '*.jinja' --local-dir "$NARWHAL_MODEL_DIR"
    hf download "$GGUF_REPO_ID" "$GGUF_FILE" --revision "$GGUF_REVISION" \
      --local-dir "$NARWHAL_MODEL_DIR"
    ```

    For a model with a multimodal projector, add the projector's file name after `"$GGUF_FILE"`. The GGUF plugin reads the projector from the directory of the GGUF file.

4. Set `NARWHAL_MODEL_PATH` in `.env` to the absolute path of the GGUF file, `$NARWHAL_MODEL_DIR/<file>.gguf`.
5. Record both repository IDs and commit SHAs in the private record.

When an engine host keeps the model in another directory, set `NARWHAL_NODE_<n>_MODEL_DIR` and `NARWHAL_NODE_<n>_MODEL_PATH` for that engine `<n>`. Every engine's directory must hold the same files, so `NARWHAL_MODEL_TREE_SHA256` covers them all.

Discovery hashes the GGUF file with the rest of the directory and writes its SHA-256 to `NARWHAL_NODE_<n>_MODEL_SHA256` in `config/deployment.env`. To have discovery check the file against a known SHA-256, set `NARWHAL_MODEL_SHA256` in `.env`.

In each launch record, discovery pins the image's `vllm-gguf-plugin` version in `runtime.expected_packages`, and records `NARWHAL_GGUF_PLUGIN_PYTHON_SHA256` and `NARWHAL_GGUF_PLUGIN_EXTENSION_SHA256` as `runtime.gguf_plugin_python_sha256` and `runtime.gguf_plugin_extension_sha256`. It adds `--load-format gguf` and `--hf-config-path /model` to `runtime.extra_args` when `NARWHAL_ENGINE_ARGS` does not set them, and sets `runtime.tokenizer` to `/model` when `NARWHAL_TOKENIZER` is unset. The container mounts `NARWHAL_MODEL_DIR` at `/model`.

When `config.json` has a `vision_config` or an `audio_config`, discovery also adds `--language-model-only`. vLLM then sets every multimodal input limit to 0, and the engine serves text prompts only. To keep multimodal input on every engine, set `NARWHAL_LANGUAGE_MODEL_ONLY=false` in `.env` and stage the multimodal projector as in step 3.

Discovery fails when:

- the image has no `vllm-gguf-plugin`
- `NARWHAL_GGUF_PLUGIN_PYTHON_SHA256` or `NARWHAL_GGUF_PLUGIN_EXTENSION_SHA256` is unset or is not a SHA-256 digest
- `NARWHAL_MODEL_PATH` names no file in `NARWHAL_MODEL_DIR`, or a file without the `.gguf` extension
- the engines' GGUF files differ in SHA-256
- `NARWHAL_ENGINE_BACKEND` selects SGLang or TensorRT-LLM

#### Building a GGUF engine image

[`deploy/vllm-gguf/Dockerfile`](https://github.com/athrael-soju/Narwhal/blob/main/deploy/vllm-gguf/Dockerfile) builds a CUDA engine image from `vllm/vllm-openai:v0.30.0`, pinned by digest. It installs `vllm-gguf-plugin` 0.0.5 and `gguf` 0.19.0, each checked against the SHA-256 in `deploy/vllm-gguf/requirements.txt`, and replaces the plugin's Python files with those at plugin revision `d4c1f0d082fc7cd4350da56689109a01c1f29d6c`, as the [dev CUDA runtime](../dev/CUDA-Runtime.md) does. The build fails unless the image holds the package versions and plugin digests of the vLLM dev templates.

Build the image once, then copy it to the engine hosts:

1. On a Linux x86-64 host with Docker, network access and a Narwhal checkout, build the image from the checkout root:

    ```bash
    docker build --progress=plain -t narwhal-vllm-gguf:0.30.0 deploy/vllm-gguf
    ```

    The build prints the plugin digests:

    ```text
    gguf_plugin_python_sha256=b08b2ffdf18e4314ed7e4a8cfd49de3520edd5d9bc9be4564d8a71b2265771e0
    gguf_plugin_extension_sha256=64521127698503e4b72053cd06adc9aa0448debb07715b2663b1250a816b2c1e
    ```

2. Copy the image to each engine host that did not build it, replacing `<engine-host>` with its SSH destination:

    ```bash
    docker save narwhal-vllm-gguf:0.30.0 | ssh <engine-host> docker load
    ```

3. On every engine host, print the image ID:

    ```bash
    docker image inspect --format '{{.Id}}' narwhal-vllm-gguf:0.30.0
    ```

    Every host must print the same ID.

4. In `.env`, set `NARWHAL_ENGINE_IMAGE` to that ID, `NARWHAL_GGUF_PLUGIN_PYTHON_SHA256` to the `gguf_plugin_python_sha256` digest, and `NARWHAL_GGUF_PLUGIN_EXTENSION_SHA256` to the `gguf_plugin_extension_sha256` digest.

`narwhal-engine check` hashes the plugin in the image and fails when either digest differs from the launch record. An image with the plugin wheel's own Python files fails this check.

## Running discovery and access checks

`access` runs once for each host of the inventory. It logs in once and keeps the host's `hostname` output in `hostname.txt`, in `stages/access/<host-id>/attempt-<n>/` of the run directory. Its inputs are the SSH destinations and roles, the password variable names, `NARWHAL_SSH_JUMP` and the SHA-256 of `NARWHAL_SSH_KNOWN_HOSTS`, so a change to any of them repeats it.

`discover` runs once on the workstation after every `access` passes. It logs in to every host, inspects the GPUs, the engine image and the checkpoint on each engine host, checks each checkpoint against `NARWHAL_MODEL_TREE_SHA256`, and writes its output to `stages/discover/attempt-<n>/`: the generated configuration in `config/` and the inspection records in `discovery/`, both listed under [Discovery output](#discovery-output). Every later stage reads these files, never the ones in your `config/` directory. Its inputs are every `NARWHAL_` variable that no later stage claims, which [Deployment run inputs](../configuration/04-Deployment-Inputs.md#deployment-run-inputs) describes, so a change to the hardware, model, image, checkpoint, launch policy, route or transport repeats discovery and every stage after it.

Each engine host needs:

- Python 3
- Docker, usable by the SSH user
- `ip`
- `rocminfo` or `nvidia-smi`
- the pinned engine image
- the checkpoint at `NARWHAL_MODEL_DIR`
- the run directory at `NARWHAL_RUN_DIR`

Each host runs every command under coreutils `timeout` and needs a writable `~/.cache/narwhal-commands/`, as [Host keys and time limits](../cli/Deploy.md#host-keys-and-time-limits) describes.

To run the stages alone, from the workstation with `.env` loaded:

```bash
narwhal deploy discover --out runs/discovery/first-deploy
. config/deployment.env
narwhal deploy access
```

Alone, `discover` writes the generated configuration to `config/`, or to the paths `NARWHAL_HOSTS`, `NARWHAL_LAUNCH_CONFIG` and `NARWHAL_FLEET` name, and its inspection records to `--out`. It prints `Generated private configuration. Load config/deployment.env before packaging.` `access` reads the inventory discovery wrote, logs in once per physical host, prints each host ID with its roles, and saves the `hostname` output under `runs/access-<id>/`.

### Discovery output

Discovery reads the checkpoint's `config.json` and `tokenizer_config.json` and sets the [custom-code and convolutional-state launch settings](../configuration/05-Engine-Launch.md#runtime-launch-records-and-image-verification).

Discovery writes the generated configuration at mode 0600, in `config/` of the `discover` attempt:

| File                                | Content                                                                                                            |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `hosts.local.json`                  | Host IDs, SSH destination and password variable names, and the router, standby router, load balancer and engine roles on each host |
| `engine-launch.local.json`          | GPU allocation, image and model metadata, transfer devices, launch policy                                          |
| `engine-launch.sources.json`        | Inspection and policy records for each role                                                                        |
| `fleet.json`                        | Model, measured hardware and TP shape, engine URL references, initial roles, initial latency targets, profile path |
| `deployment.env`                    | Generated paths, fabric addresses, engine and attestation URLs, per-engine image and model-file hash values        |

The inspection records, in `discovery/` at mode 0700:

| File                                                         | Content                                                                                                                                                                                                                                                                       |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `engine-<n>.json`                                            | GPU products and device mappings, image identity and package versions, model-config hash and dtype, custom-code, convolutional-state and `multimodal` flags, fabric interface addresses, image runtime environment, and for a GGUF model `model_file` and `model_file_sha256` |
| `engine-<n>-checkpoint.json`                                 | Path, size, and SHA-256 of every checkpoint file, the exclusion policy, and `model_tree_sha256`                                                                                                                                                                               |
| `logs/`                                                      | SSH command logs                                                                                                                                                                                                                                                              |
| `engine-<n>-inputs.json`, `engine-launch.json`, `hosts.json` | Probe inputs and the candidate launch and host records                                                                                                                                                                                                                        |
| `manifest.json`                                              | Hashes of the generated configuration files and of `NARWHAL_SSH_KNOWN_HOSTS`, the engine roles, and the shared `model_tree_sha256`                                                                                                                                            |

## Confirming the launch policy

A GPU host with one engine role gives that engine every detected GPU, with tensor parallelism set to that count. For several engine roles on one GPU host, declare disjoint `NARWHAL_NODE_<n>_GPU_IDS` lists. Every replica must have the same accelerator product, GPU count, and TP size.

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
| Profile path           | `runs/profiles.json` in the router's deployment directory      |

Override the policy in `.env` before discovery:

| Field                                                                                    | Meaning                                                                                                                       |
| ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `NARWHAL_ENGINE_BACKEND`, `NARWHAL_ENGINE_CONNECTOR`                                     | Fleet `engine.backend` (`vllm`, the default, `sglang` or `trtllm`) and `engine.connector`                                     |
| `NARWHAL_DECODE_CUDA_GRAPH_MEMORY_GB`                                                    | GB of decode CUDA graph memory for each SGLang engine, required with the `mooncake` connector                                 |
| `NARWHAL_GPU_IDS`, `NARWHAL_TENSOR_PARALLEL_SIZE`                                        | Comma-separated GPU indices or NVIDIA UUIDs, and the replica TP size                                                          |
| `NARWHAL_MODEL_DTYPE`, `NARWHAL_BLOCK_SIZE`                                              | Model dtype (`bfloat16` or `float16`) and requested cache block size                                                          |
| `NARWHAL_CONTEXT_LENGTH`, `NARWHAL_MAX_SEQUENCES`                                        | `runtime.context_length` and `runtime.max_sequences`                                                                          |
| `NARWHAL_PREFIX_CACHING`, `NARWHAL_KV_EVENTS`, `NARWHAL_TOKENIZER`                       | `runtime.prefix_caching` and `runtime.kv_events`, each `true` or `false`, and `runtime.tokenizer`                             |
| `NARWHAL_LANGUAGE_MODEL_ONLY`                                                            | `true` or `false` for the fleet; `false` keeps multimodal GGUF input on vLLM. Discovery refuses a per-engine override         |
| `NARWHAL_ENGINE_ARGS`                                                                    | JSON array that replaces the default `runtime.extra_args`                                                                     |
| `NARWHAL_ENGINE_ENV`                                                                     | JSON object of launcher-supported environment fields that overrides the image environment                                     |
| `NARWHAL_TRANSFER_TRANSPORT`, `NARWHAL_TRANSFER_NET_DEVICES`, `NARWHAL_TRANSFER_DEVICES` | Transport (`ucx_tcp` or `ucx_rdma`), UCX network devices (`HCA:port` entries for RDMA), and a JSON array of RDMA device paths |
| `NARWHAL_TTFT_S`, `NARWHAL_TPOT_S`                                                       | Initial TTFT and TPOT limits, in seconds                                                                                      |
| `NARWHAL_ENGINE_KEY`                                                                     | Engine API key that discovery names in `engine.engine_api_key_env`                                                            |

Per-engine overrides use `NARWHAL_NODE_<n>_<field>`, such as `NARWHAL_NODE_2_GPU_IDS`.

With `NARWHAL_ENGINE_BACKEND=sglang`, discovery writes the [SGLang runtime fields](../configuration/05-Engine-Launch.md#sglang-runtime-fields) and requires the `sglang`, `torch` and `mooncake-transfer-engine` versions installed in the image, including CUDA builds such as `mooncake-transfer-engine-cuda13`. The cache block, execution, maximum sequence and GPU memory defaults apply to vLLM only. For SGLang, `NARWHAL_ENGINE_ARGS` defaults to an empty list, and discovery writes [`runtime.max_sequences`](../configuration/05-Engine-Launch.md#launch-settings-for-every-backend) only when you set `NARWHAL_MAX_SEQUENCES`, from 1 to 256.

With `NARWHAL_ENGINE_BACKEND=trtllm`, discovery writes the [TensorRT-LLM runtime fields](../configuration/05-Engine-Launch.md#tensorrt-llm-runtime-fields) and requires the `tensorrt_llm` and `torch` versions installed in the image. `NARWHAL_ENGINE_ARGS` defaults to an empty list, and discovery writes [`runtime.max_sequences`](../configuration/05-Engine-Launch.md#launch-settings-for-every-backend) only when you set `NARWHAL_MAX_SEQUENCES`.

Discovery writes the maximum context to `runtime.context_length` and `NARWHAL_ENGINE_ARGS` to `runtime.extra_args`. It fails when `NARWHAL_ENGINE_ARGS` sets a flag that one of the [launch settings for every backend](../configuration/05-Engine-Launch.md#launch-settings-for-every-backend) controls.

## Access failure handling

A failed remote command prints `<host-id>: blocked at <operation>; inspect <log>`. When discovery cannot hash a model directory, the message names the reason after the operation, as in `<host-id>: blocked at hash checkpoint files: checkpoint entry resolves outside the model directory: <file>; inspect <log>`. A command that exceeds its stage's time limit prints `<host-id>: <operation> exceeded the <limit> <stage> time limit; inspect <log>`; [Host keys and time limits](../cli/Deploy.md#host-keys-and-time-limits) lists the limits and how to raise one.

If an input is missing, set the variable the message names.

If the message says that the host key differs from `NARWHAL_SSH_KNOWN_HOSTS` or is missing from it:

1. Verify the destination and fingerprint independently, such as through the provider console.
2. Add or replace the entry in `NARWHAL_SSH_KNOWN_HOSTS`.

If authentication or the connection fails, check the username, credential, route, SSH port, and firewall.

Then run `narwhal deploy run` again. It repeats `access` for the hosts whose inputs changed, and resumes at the stage that failed.

[Install Narwhal on the hosts](02-Install.md)
