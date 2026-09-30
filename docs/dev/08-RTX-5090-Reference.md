# RTX 5090 reference for Narwhal dev

This page covers a reference setup for one RTX 5090 running four
Qwen3.5-0.8B GGUF engines. Prepare the host with
[Prepare Ubuntu or WSL2](01-Prepare-Host.md) and
[Install the CUDA runtime and model](02-CUDA-Runtime.md), and keep the
instance's virtual environment active for every command.

Settings for the reference setup:

- Each engine gets 0.1 of the card's memory, a 4,096-token context limit, and
  up to four active sequences.
- The startup memory allowance for the whole card is 0.5 of total VRAM.
- `init` requires another 2,048 MiB of free VRAM on top of that allowance.

## Export the template

The reference template ships inside the package. Export it into your Linux
checkout:

```bash
mkdir -p runs
python - <<'PYTHON' > runs/rtx5090-template.json
from importlib.resources import files
print(files('narwhal.dev').joinpath('reference-v1.json').read_text())
PYTHON
```

The template uses the same runtime and model hashes as the default installed
template (the small-GPU template, see [Templates](03-Templates.md)). It pins
the GPU product and requires at least 30,000 MiB of total VRAM, so it does not
start on a different GPU product or a card below that size.

## Launch and verify

Set `--interface` to your network interface. List candidates with
`ip -brief -4 address` and pick one with a single IPv4 address.

```bash
narwhal dev init --interface eth0 --template runs/rtx5090-template.json
narwhal dev up
narwhal dev verify
narwhal dev status
```

With four engines, `verify` tests 12 directed KV transfers, one for each
ordered pair of engines, and ends with an arithmetic request through the
router.

The router listens on `127.0.0.1:18000`. Engine HTTP ports start at 18101,
attestation ports at 18201, and NIXL side-channel ports at 5701. See
[Run and stop an instance](04-Run-Instance.md) to change ports or use
a different instance directory.

Send a test request through the router:

```bash
curl http://127.0.0.1:18000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.5-0.8B-GGUF-Q4_K_M","messages":[{"role":"user","content":"Reply with only the number: 2 + 3 = ?"}],"temperature":0,"max_tokens":32}'
```

The reply should be `5`. Metrics endpoints are available on Ubuntu and WSL2.
[WSL2 monitoring](../observability/04-WSL2.md) forwards metrics to Prometheus
and Grafana on another machine.

## Replay the role cycle

The `role_cycle` section of the template fixes the token pool, random seeds,
and workload order, so replays are repeatable. Run the replay from the same
Narwhal checkout, starting from a verified fleet with two prefill and two
decode engines (2P:2D). The runner waits out the 30-second demand window and
sends one warmup request before each phase. After `up` and `verify`, the
workload takes about two minutes.

```bash
python -m tools.measurement.dev_cycle --instance runs/dev --dry-run
python -m tools.measurement.dev_cycle --instance runs/dev
narwhal dev down --instance runs/dev
```

| Phase          | Input / output tokens | Requests | Requests/s | Max in flight |
| -------------- | --------------------- | -------- | ---------- | ------------- |
| Decode         | 256 / 128             | 24       | 0.5        | 8             |
| Prefill steady | 3,840 / 1             | 35       | 1          | 8             |
| Prefill burst  | 3,840 / 1             | 12       | 100        | 12            |

Narwhal picks roles from the current profiles and the work already running on
each engine. Across the whole run, the runner expects the controller to move
from 2P:2D to 1P:3D, back to 2P:2D, on to 3P:1D, and finally back to 2P:2D.

- Steady phases: every request must meet the template's TTFT (time to first
  token) and TPOT (time per output token) budgets.
- Burst phase: completed requests and HTTP 429 responses count. The
  router sends 429 when it predicts a request would miss the TTFT budget.
  The burst phase is judged more loosely, and its latency results are
  recorded separately.

Different engine profiles or other work on the GPU can change which transitions
happen and how fast.

Each replay writes a `cycle-*` directory inside the instance, or in the new
directory named by `--out`. It contains:

- `summary.json`: the splits the controller chose, latency for each phase,
  whether the run passed, and a `grafana_range` for the dashboard's `from`
  and `to` URL parameters.
- A copy of the template, the fleet as it ran, source hashes, every request,
  and the router state.

Exit codes:

| Code | Meaning                                                      |
| ---- | ------------------------------------------------------------ |
| 0    | The cycle and the steady-phase budgets passed.               |
| 2    | The replay finished but missed the cycle or budgets.         |
| 1    | The runner could not set up or run.                          |

## Profiling and controller settings

At startup Narwhal profiles the 1P:3D, 2P:2D, and 3P:1D splits, so the
controller can estimate the cost of moving in either direction from the current
split. The prefill and decode sweeps cover inputs from 128 to
3,840 tokens, decode concurrency of one and two, and up to 128 output tokens.
Input and output together must fit in the 4,096-token context.

Controller settings in the template:

- TTFT budget: 1 s. TPOT budget: 125 ms. Both are tighter than the small-GPU
  template.
- Engine sampling interval: 100 ms.
- Role-change decision interval: 250 ms.
- Demand window: 30 s.
- Confirmations required before a standard role change: 3.

After `verify`, send a full demand window of representative traffic, then
judge the controller's role decisions.

## Adapt the template

To change the engine count, model, context length, or memory fractions, edit
`runs/rtx5090-template.json` and run `narwhal dev init --template` on it for a
new instance (see [Run and stop an instance](04-Run-Instance.md) for instance
directories). Then run `narwhal dev up` and `narwhal dev verify`, which check
startup memory, KV transfers between engines, and routed completions.
