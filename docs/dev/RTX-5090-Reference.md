# RTX 5090 reference for Narwhal dev

This recipe runs four Qwen3.5-0.8B GGUF engines on one RTX 5090. Complete
the host setup in [Narwhal dev](../Dev-Runtime.md) and the shared
[CUDA runtime and model installation](CUDA-Runtime.md) first, on Ubuntu or
Ubuntu under WSL2.

Each of the four engines gets a 4,096-token context limit (input plus
output), four active sequences, and a 0.1 vLLM memory fraction. Together
they may use 0.5 of the device. The launch checks for `narwhal dev init`
also require 2,048 MiB of free VRAM.

## Select the RTX 5090 template

Export the packaged reference template:

```bash
mkdir -p runs
python - <<'PYTHON' > runs/rtx5090-template.json
from importlib.resources import files
print(files('narwhal.dev').joinpath('reference-v1.json').read_text())
PYTHON
```

The template pins the GPU product and a 30,000 MiB minimum. Its runtime and
model hashes match the installed small-GPU template.

## Launch and verify the reference

Check `ip -brief -4 address` to find the Linux network interface that
carries a single IPv4 address, and use its name in place of `eth0` where
needed:

```bash
narwhal dev init --interface eth0 --template runs/rtx5090-template.json
narwhal dev up
narwhal dev verify
narwhal dev status
```

`up` profiles the engines and starts the router. For this four-engine
reference, `verify` checks all 12 eligible directed KV transfers and one
routed arithmetic request, then reports `ready`. Use the same virtual
environment for later lifecycle commands. The instance records its
interpreter.

The router listens on `127.0.0.1:18000`. Engine HTTP ports start at 18101,
attestation ports at 18201, and NIXL side-channel ports at 5701. Pass
`narwhal dev init --port-base` to choose a different port layout. Pass
`--instance` on each command to address another instance.

Send a routed chat completion:

```bash
curl http://127.0.0.1:18000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.5-0.8B-GGUF-Q4_K_M","messages":[{"role":"user","content":"Reply with only the number: 2 + 3 = ?"}],"temperature":0,"max_tokens":32}'
```

The response content should be `5`. The local metrics endpoints work on
Ubuntu and WSL2. To forward them to a separate Prometheus and Grafana host,
see [the WSL2 monitoring example](../observability/04-WSL2.md).

## Replay all three role splits

The `role_cycle` in the reference template fixes the token pool, random
seeds, and workload order. Run it against a verified instance that starts
with two prefill and two decode engines (2P:2D) and an active role
controller, from the matching Narwhal checkout with the instance's virtual
environment active.

Run the replay and stop the instance:

```bash
python -m tools.measurement.dev_cycle --instance runs/dev --dry-run
python -m tools.measurement.dev_cycle --instance runs/dev
narwhal dev down --instance runs/dev
```

The runner lets the 30-second demand window expire, then sends one warmup
request before each phase.

| Phase | Input / output tokens | Requests | Requests/s | Maximum in flight |
| --- | --- | --- | --- | --- |
| Decode | 256 / 128 | 24 | 0.5 | 8 |
| Prefill steady | 3,840 / 1 | 35 | 1 | 8 |
| Prefill burst | 3,840 / 1 | 12 | 100 | 12 |

The role controller moves through 2P:2D → 1P:3D → 2P:2D → 3P:1D → 2P:2D.
Every steady-phase request meets the template's time to first token (TTFT)
and time per output token (TPOT) budgets. The burst phase accepts completed
requests and HTTP 429 responses that cite the TTFT budget. The runner
records burst latency attainment separately. Transitions and latency vary
with engine profiles and other GPU work. The workload takes about two
minutes after `up` and `verify`.

Each replay creates a `cycle-*` directory beneath the instance, or a fresh
directory when you pass `--out`. The `summary.json` inside records the
observed splits, per-phase latency, and the acceptance result. Its
`grafana_range` timestamps map to the dashboard's `from` and `to` URL
parameters. The rest of the directory preserves the template, effective
fleet, source hashes, request rows, and router state.

Exit codes:

- 0: the cycle and steady-phase budgets passed.
- 2: a completed replay failed those checks.
- 1: setup or execution failed.

## Reference operating limits

The router serves state and metrics locally:

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

The four engines open with two prefill and two decode roles. Startup
profiles the 1P:3D, 2P:2D, and 3P:1D splits so the controller can price
changes in both directions from the current processes. The prefill and
decode sweeps cover 128 to 3,840 input tokens. Decode runs at concurrency
one and two with up to 128 output tokens. The 4,096-token engine context
limit bounds every sweep.

The reference applies a 1-second TTFT budget and a 125 ms TPOT budget.
Narwhal samples engines every 100 ms and evaluates role changes every
250 ms. Ordinary moves use a 30-second demand window and need three
confirmations. After `verify`, fill one demand window with representative
traffic before assessing role changes.

To change the engine count, model, context length, or memory fractions,
edit the exported `runs/rtx5090-template.json` and initialize a fresh
instance from that file. Then run `up` and `verify` to measure the new
instance on the target GPU.
