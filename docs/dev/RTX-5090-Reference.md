# RTX 5090 reference for Narwhal dev

The installed Narwhal dev template pins an RTX 5090, Qwen3.5-0.8B GGUF,
four engines and the runtime versions below. This is the measured reference
recipe. Run it on Ubuntu or Ubuntu under WSL2 after following the host setup
in [Narwhal dev](../Dev-Runtime.md). The same Python and lifecycle commands
run in either environment.

The reference allocates four engines with a 4,096-token context limit, four
active sequences per engine and a 0.1 vLLM memory fraction each. Launch
checks reserve 2,048 MiB of free VRAM beyond the 0.5 whole-device allowance.

## Install the reference runtime

From the Narwhal checkout in the Linux shell:

```bash
python3.12 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install .
python -m pip install 'vllm==0.29.0' 'torch==2.13.0' \
  'transformers==5.17.0' 'nixl==1.4.1' 'nixl-cu13==1.4.1'
python -m pip install \
  'https://github.com/vllm-project/vllm-gguf-plugin/releases/download/v0.0.5/vllm_gguf_plugin-0.0.5-cp310-abi3-manylinux_2_28_x86_64.whl'
```

Apply the pinned GGUF loader sources over the wheel's Python files while
keeping its CUDA extension:

```bash
mkdir -p runs
git clone https://github.com/vllm-project/vllm-gguf-plugin.git runs/gguf-plugin
git -C runs/gguf-plugin checkout d4c1f0d082fc7cd4350da56689109a01c1f29d6c
python - <<'PY'
from importlib.metadata import distribution
from pathlib import Path
import shutil

source = Path('runs/gguf-plugin/vllm_gguf_plugin')
target = Path(distribution('vllm-gguf-plugin').locate_file('vllm_gguf_plugin'))
for path in source.rglob('*.py'):
    destination = target / path.relative_to(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, destination)
PY
```

`narwhal dev init` checks the plugin's Python tree and CUDA extension hashes
against the installed template. Reapply these pinned sources after
reinstalling the plugin wheel.

## Download the reference model and tokenizer

```bash
hf download unsloth/Qwen3.5-0.8B-GGUF \
  --revision e524882462b3f2a9fe83be967c654c4322abb2f6 \
  Qwen3.5-0.8B-Q4_K_M.gguf
hf download Qwen/Qwen3.5-0.8B \
  --revision 2fc06364715b967f1860aea9cf38778875588b17 \
  --include '*.json' '*.txt' '*.jinja'
```

The template resolves these revisions in the Hugging Face cache. For a
custom cache location, pass the GGUF file with `--model` and the
configuration/tokenizer directory with `--model-dir`.

## Launch and verify the reference

Select the Linux network interface that has one IPv4 address with
`ip -brief -4 address`, then use its name in place of `eth0` if needed:

```bash
narwhal dev init --interface eth0
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

Changing engine count, model, context length or memory fractions requires a
matching template and a fresh `up` and `verify` cycle. Export the installed
reference to edit it:

```bash
python - <<'PY' > runs/dev-template.json
from importlib.resources import files
print(files('narwhal.dev').joinpath('reference-v1.json').read_text())
PY
narwhal dev init --instance runs/dev-custom --template runs/dev-template.json
```

The product name and 30,000 MiB minimum in that template record the
reference hardware. A smaller GPU needs a separately measured template and
runtime configuration that fit two engines, model weights, KV cache and the
free-memory reserve.
