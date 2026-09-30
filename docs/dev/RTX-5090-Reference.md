---
description: Launch and verify four Qwen3.5-0.8B engines on one RTX 5090 with Narwhal dev.
---

# RTX 5090 reference for Narwhal dev

Four Qwen3.5-0.8B GGUF engines run on one RTX 5090, on Ubuntu or Ubuntu under WSL2.

Prerequisites:

1. Complete the host setup in [Narwhal dev](../Dev-Runtime.md).
2. Complete the shared [CUDA runtime and model installation](CUDA-Runtime.md).

| Setting | Value |
| --- | --- |
| Context limit per engine | 4,096 tokens, input plus output |
| Active sequences per engine | 4 |
| vLLM memory fraction per engine | 0.1 |
| Device share for all four engines | Up to 0.5 |
| Free VRAM required by `narwhal dev init` | Half the total VRAM plus 2,048 MiB |

## Select the RTX 5090 template

Export the packaged reference template:

```bash
mkdir -p runs
python - <<'PYTHON' > runs/rtx5090-template.json
from importlib.resources import files
print(files('narwhal.dev').joinpath('reference-v1.json').read_text())
PYTHON
```

The template pins:

- the GPU product, NVIDIA GeForce RTX 5090
- a minimum of 30,000 MiB total VRAM
- the same runtime and model hashes as the installed small-GPU template

## Launch and verify the reference

1. Run `ip -brief -4 address`.
2. Find the Linux network interface that carries a single IPv4 address.
3. Replace `eth0` with that interface name in these commands:

    ```bash
    narwhal dev init --interface eth0 --template runs/rtx5090-template.json
    narwhal dev up
    narwhal dev verify
    narwhal dev status
    ```

| Command | Result |
| --- | --- |
| `up` | Profiles the engines and starts the router |
| `verify` | Checks the 12 eligible directed KV transfers and one routed arithmetic request, and reports `ready` |

Run later lifecycle commands in the virtual environment that ran `init`.

Default ports:

| Service | Port |
| --- | --- |
| Router | `127.0.0.1:18000` |
| Engine HTTP | 18101 and up |
| Attestation | 18201 and up |
| NIXL side channel | 5701 and up |

Options for another layout or instance:

| Option | Effect |
| --- | --- |
| `narwhal dev init --port-base` | Selects a different port layout |
| `--instance` on each command | Addresses another instance |

Send a routed chat completion:

```bash
curl http://127.0.0.1:18000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.5-0.8B-GGUF-Q4_K_M","messages":[{"role":"user","content":"Reply with only the number: 2 + 3 = ?"}],"temperature":0,"max_tokens":32}'
```

The expected response content is `5`.

Forward metrics to Prometheus and Grafana with the [WSL2 monitoring example](../observability/04-WSL2.md).

## Replay all three role splits

The `role_cycle` in the reference template fixes the token pool, random seeds, and workload order.

Replay prerequisites:

- a verified instance that starts with two prefill and two decode engines (2P:2D)
- an active role controller
- the matching Narwhal checkout as the working directory
- the instance's virtual environment as the active environment

Run the replay and stop the instance:

```bash
python -m tools.measurement.dev_cycle --instance runs/dev --dry-run
python -m tools.measurement.dev_cycle --instance runs/dev
narwhal dev down --instance runs/dev
```

Replay phases:

| Phase | Input / output tokens | Requests | Requests/s | Maximum in flight |
| --- | --- | :---: | :---: | :---: |
| Decode | 256 / 128 | 24 | 0.5 | 8 |
| Prefill steady | 3,840 / 1 | 35 | 1 | 8 |
| Prefill burst | 3,840 / 1 | 12 | 100 | 12 |

| Item | Value |
| --- | --- |
| Split sequence | 2P:2D, 1P:3D, 2P:2D, 3P:1D, 2P:2D |
| Duration | About two minutes |

Acceptance criteria:

| Scope | Accepted outcome |
| --- | --- |
| Role cycle | The observed splits match the split sequence, with every move made by the role controller on the same engine processes. |
| Decode and Prefill steady | Every request meets the template's time to first token (TTFT) and time per output token (TPOT) budgets. |
| Prefill burst | Every request completes or returns an HTTP 429 response that cites the TTFT budget. |

Replay records directory:

| Option | Directory |
| --- | --- |
| Default | A `cycle-*` directory beneath the instance |
| `--out DIR` | `DIR`, a fresh directory |

| Content | Holds |
| --- | --- |
| `summary.json` | Observed splits, per-phase latency, and the acceptance result |
| `grafana_range` in `summary.json` | Timestamps for the dashboard's `from` and `to` URL parameters |
| Other files | The template, effective fleet, source hashes, request rows, and router state |

Exit codes:

| Code | Meaning |
| :--: | --- |
| `0` | The role cycle and every phase passed. |
| `1` | Setup or execution failed. |
| `2` | A completed replay failed the role cycle or a phase. |

## Reference operating limits

State and metrics endpoints:

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

| Setting | Value |
| --- | --- |
| Initial split | 2P:2D |
| Profiled splits at startup | 1P:3D, 2P:2D, and 3P:1D |
| Prefill and decode sweep input | 128 to 3,840 tokens |
| Decode sweep concurrency | 1 and 2 |
| Decode sweep output | Up to 128 tokens |
| Sweep bound | The 4,096-token engine context limit |
| TTFT budget | 1 second |
| TPOT budget | 125 ms |
| Engine sampling interval | 100 ms |
| Role-change evaluation interval | 250 ms |
| Demand window for ordinary moves | 30 seconds |
| Confirmations for ordinary moves | 3 |

Fill one 30-second demand window with representative traffic between `verify` and any assessment of role changes.

To change the engine count, model, context length, or memory fractions:

1. Edit the exported `runs/rtx5090-template.json`.
2. Initialize a fresh instance from that file.
3. Run `up` and `verify` on the target GPU.
