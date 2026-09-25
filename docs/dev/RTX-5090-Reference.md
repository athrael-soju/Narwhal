# RTX 5090 reference for Narwhal dev

This measured recipe pins an RTX 5090, Qwen3.5-0.8B GGUF and four engines.
Follow the host setup in [Narwhal dev](../Dev-Runtime.md) and the shared
[CUDA runtime and model installation](CUDA-Runtime.md) on Ubuntu or Ubuntu
under WSL2.

The reference allocates four engines with a 4,096-token context limit, four
active sequences per engine and a 0.1 vLLM memory fraction each. Launch
checks reserve 2,048 MiB of free VRAM beyond the 0.5 whole-device allowance.

## Select the RTX 5090 template

Export the packaged reference into the Linux checkout before initializing an
instance:

```bash
mkdir -p runs
python - <<'PYTHON' > runs/rtx5090-template.json
from importlib.resources import files
print(files('narwhal.dev').joinpath('reference-v1.json').read_text())
PYTHON
```

The template pins the GPU product, a 30,000 MiB minimum and the same runtime
and model hashes as the installed small-GPU template.

## Launch and verify the reference

Select the Linux network interface that has one IPv4 address with
`ip -brief -4 address`, then use its name in place of `eth0` if needed:

```bash
narwhal dev init --interface eth0 --template runs/rtx5090-template.json
narwhal dev up
narwhal dev verify
narwhal dev status
```

`up` profiles the engines and starts the router. For this four-engine
reference, `verify` checks all 12 eligible directed KV transfers and a
routed arithmetic request. Use the same virtual environment for subsequent
lifecycle commands; the instance records its interpreter.

The router listens on `127.0.0.1:18000`. Engine HTTP ports start at 18101,
attestation ports at 18201 and NIXL side-channel ports at 5701. Select
another port layout with `narwhal dev init --port-base`, or another instance
with `--instance` on each command.

```bash
curl http://127.0.0.1:18000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.5-0.8B-GGUF-Q4_K_M","messages":[{"role":"user","content":"Reply with only the number: 2 + 3 = ?"}],"temperature":0,"max_tokens":32}'
```

Expect the response content `5`. The local metrics endpoints work on Ubuntu
and WSL2; [the WSL2 monitoring example](../observability/04-WSL2.md)
forwards them to a separate Prometheus and Grafana host.

## Replay all three role splits

The reference template's `role_cycle` fixes the token pool, random seeds and
workload order. From the matching Narwhal checkout, with the instance's
virtual environment active:

```bash
python -m tools.measurement.dev_cycle --instance runs/dev --dry-run
python -m tools.measurement.dev_cycle --instance runs/dev
narwhal dev down --instance runs/dev
```

Start from a verified 2P:2D fleet. The runner lets the 30-second demand
window expire, then sends one warmup request before each phase:

| Phase | Input / output tokens | Requests | Requests/s | Maximum in flight |
| --- | --- | --- | --- | --- |
| Decode | 256 / 128 | 24 | 0.5 | 8 |
| Prefill steady | 3,840 / 1 | 35 | 1 | 8 |
| Prefill burst | 3,840 / 1 | 12 | 100 | 12 |

Narwhal chooses roles from the current profiles and resident work throughout
the sequence. The runner checks controller-selected
**2P:2D → 1P:3D → 2P:2D → 3P:1D → 2P:2D** transitions and requires every
steady-phase request to meet the template's TTFT and TPOT budgets. The burst
accepts completed requests and TTFT-budget HTTP 429 responses, and records
its latency attainment separately. Engine profiles and concurrent GPU work
can change the resulting transitions and latency.

Allow about two minutes for the workload sequence after `up` and `verify`.
Each replay creates `cycle-*` beneath the instance, or a fresh directory
selected with `--out`. Its `summary.json` contains the observed splits,
per-phase latency, acceptance result and `grafana_range` timestamps for the
dashboard's `from` and `to` URL parameters. The directory also preserves
the template, effective fleet, source hashes, request rows and router state.
Exit code 0 means the cycle and steady-phase budgets passed; 2 means a
completed replay failed those checks; 1 means setup or execution failed.

## Inspect the reference's operating limits

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

The four engines open with two prefill and two decode roles. Startup
profiles 1P:3D, 2P:2D and 3P:1D so the controller can price changes in both
directions from the current processes. Prefill and decode sweeps cover
128 to 3,840 input tokens, with decode concurrency one and two and up to
128 output tokens. The 4,096-token engine context limit bounds input plus
output.

The reference uses a 1-second TTFT budget and a 125-ms TPOT budget. Narwhal
samples engines every 100 ms and evaluates role changes every 250 ms, using
a 30-second demand window and three confirmations for ordinary moves. After
`verify`, fill one demand window with representative traffic before
assessing role changes.

To change engine count, model, context length or memory fractions, edit the
exported `runs/rtx5090-template.json` and initialize a fresh instance with
that file. Run `up` and `verify` to measure startup memory, directed KV paths
and routed completion on the target GPU.
