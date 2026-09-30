# RTX 5090 reference for Narwhal dev

This recipe was measured on an RTX 5090. It runs four Qwen3.5-0.8B GGUF
engines on the one card. Before starting, set up the host with
[Prepare Ubuntu or WSL2](01-Prepare-Host.md) and
[Install the CUDA runtime and model](02-CUDA-Runtime.md).

Each engine gets 0.1 of the card's memory, a 4,096-token context limit, and
up to four active sequences. The startup allowance for the whole card is
0.5, and `init` insists on another 2,048 MiB of free VRAM on top of
that.

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

It uses the same runtime and model hashes as the installed small-GPU
template. Unlike that template, it pins the GPU product and requires at
least 30,000 MiB of total VRAM, so it won't start on a different card.

## Launch and verify

Replace `eth0` with your interface if it's different. Run
`ip -brief -4 address` and pick one with a single IPv4 address.

```bash
narwhal dev init --interface eth0 --template runs/rtx5090-template.json
narwhal dev up
narwhal dev verify
narwhal dev status
```

With four engines, `verify` has 12 directed KV transfers to test, one for
each ordered pair of engines, and it finishes by sending an arithmetic
request through the router. Keep the same virtual environment active for
later commands.

The router listens on `127.0.0.1:18000`. Engine HTTP ports start at 18101,
attestation ports at 18201, and NIXL side-channel ports at 5701. See
[Run and stop an instance](04-Run-Instance.md) for changing ports or using
another instance directory.

Try a request yourself:

```bash
curl http://127.0.0.1:18000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.5-0.8B-GGUF-Q4_K_M","messages":[{"role":"user","content":"Reply with only the number: 2 + 3 = ?"}],"temperature":0,"max_tokens":32}'
```

The reply should be `5`. The metrics endpoints work on both Ubuntu and
WSL2. If you want dashboards, [the WSL2 monitoring example](../observability/04-WSL2.md)
forwards the metrics to Prometheus and Grafana on another machine.

## Replay the role cycle

The template includes a `role_cycle` section that fixes the token pool, the
random seeds, and the order of the workloads, so every replay drives the
controller the same way. Run it from the same Narwhal checkout, with the
instance's virtual environment active:

```bash
python -m tools.measurement.dev_cycle --instance runs/dev --dry-run
python -m tools.measurement.dev_cycle --instance runs/dev
narwhal dev down --instance runs/dev
```

The fleet should start verified, at two prefill and two decode engines. The
runner waits out the 30-second demand window and then sends one warmup
request before each phase:

| Phase          | Input / output tokens | Requests | Requests/s | Max in flight |
| -------------- | --------------------- | -------- | ---------- | ------------- |
| Decode         | 256 / 128             | 24       | 0.5        | 8             |
| Prefill steady | 3,840 / 1             | 35       | 1          | 8             |
| Prefill burst  | 3,840 / 1             | 12       | 100        | 12            |

Narwhal picks roles from the current profiles and the work already running
on each engine. Across the whole run, the runner expects the controller to
move from 2P:2D to 1P:3D, back to 2P:2D, on to 3P:1D, and finally back to
2P:2D. Every request in the steady phases has to meet the template's TTFT
and TPOT budgets. The burst phase is judged more loosely: completed
requests count, and so do HTTP 429 responses the router sends when it
predicts a request would miss the TTFT budget. Its latency results are
recorded separately. Different engine
profiles, or other work on the GPU, can change which transitions happen and
how fast.

After `up` and `verify`, the workload takes about two minutes. Each replay
writes a `cycle-*` directory inside the instance, or in the new directory you
name with `--out`. Its `summary.json` holds the splits the controller chose,
latency for each phase, whether the run passed, and a `grafana_range` you
can drop into the dashboard's `from` and `to` URL parameters. The directory also
keeps a copy of the template, the fleet as it actually ran, source hashes,
every request, and the router state.

The runner exits with 0 if the cycle and the steady-phase budgets passed,
2 if the replay finished but missed them, and 1 if it couldn't set up or
run at all.

## How the reference behaves

The four engines start as two prefill and two decode. At startup Narwhal
profiles the 1P:3D, 2P:2D, and 3P:1D splits, so the controller can estimate
the cost of moving in either direction from wherever it is. The prefill and
decode sweeps cover inputs from 128 to 3,840 tokens, decode concurrency of
one and two, and up to 128 output tokens. Input and output together must
fit in the 4,096-token context.

Latency budgets are tighter than the small-GPU template: 1 second for TTFT
and 125 ms for TPOT. Narwhal samples the engines every 100 ms and considers
role changes every 250 ms. It looks at a 30-second demand window and wants
three confirmations before an ordinary move. So after `verify`, give it a
full window of representative traffic before you judge its role decisions.

To change the engine count, model, context length, or memory fractions, edit
`runs/rtx5090-template.json` and initialize a new instance from it. Then run
`up` and `verify` to see how startup memory, the KV paths, and routed
completions hold up on your card.
