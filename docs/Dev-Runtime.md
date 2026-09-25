# Set up the WSL2 GPU runtime

Use Ubuntu under WSL2, Python 3.12 and an RTX 5090 with 32 GB VRAM.
Install the NVIDIA Windows driver and run these commands in the WSL2 shell:

```bash
nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv
uname -r
```

Keep the model, virtual environment and instance directory on the WSL2
Linux filesystem. The default allocation uses four engines, a 4,096-token
context limit, four active sequences per engine and a 0.1 vLLM memory
fraction. Launch checks reserve another 2,048 MiB of free VRAM and enforce
an aggregate device allowance of 0.5.

## Install the runtime

From the Narwhal checkout:

```bash
python3.12 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install .
python -m pip install 'vllm==0.29.0' 'torch==2.13.0' \
  'transformers==5.17.0' 'nixl==1.4.1' 'nixl-cu13==1.4.1'
python -m pip install \
  'https://github.com/vllm-project/vllm-gguf-plugin/releases/download/v0.0.5/vllm_gguf_plugin-0.0.5-cp310-abi3-manylinux_2_28_x86_64.whl'
```

Apply the pinned GGUF loader sources over the wheel's Python files,
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

`dev init` checks the plugin's Python tree and CUDA extension hashes against
the installed template. Reapply these pinned sources after reinstalling the
plugin wheel.

## Download the model and tokenizer

```bash
hf download unsloth/Qwen3.5-0.8B-GGUF \
  --revision e524882462b3f2a9fe83be967c654c4322abb2f6 \
  Qwen3.5-0.8B-Q4_K_M.gguf
hf download Qwen/Qwen3.5-0.8B \
  --revision 2fc06364715b967f1860aea9cf38778875588b17 \
  --include '*.json' '*.txt' '*.jinja'
```

The default template resolves these revisions in the Hugging Face cache.
For a custom cache location, pass the GGUF file with `--model` and the
configuration/tokenizer directory with `--model-dir`.

## Launch and verify

```bash
narwhal dev init
narwhal dev up
narwhal dev verify
narwhal dev status
```

`up` starts and profiles the engines, then starts the router and reports
`launched`. `verify` runs all 12 eligible directed KV transfers, checks the
current engine profiles, and sends a routed arithmetic request before
reporting `ready`.

Keep this virtual environment active for every lifecycle command. The
instance records its interpreter and requires the same runtime on restart.

The router listens on `127.0.0.1:18000`. Engine HTTP ports start at 18101,
attestation ports at 18201, and NIXL side-channel ports at 5701. Select
another port layout with `dev init --port-base`, or another instance with
`--instance` on each command.

```bash
curl http://127.0.0.1:18000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.5-0.8B-GGUF-Q4_K_M","messages":[{"role":"user","content":"Reply with only the number: 2 + 3 = ?"}],"temperature":0,"max_tokens":32}'
```

Expect the response content `5`.

## Connect Prometheus and Grafana

Follow [Monitor a WSL2 development fleet](observability/04-WSL2.md) to connect
the local metrics endpoints to the homelab's canonical monitoring stack.
Check all five scrape targets and the Narwhal Orchestrator dashboard before
running workloads.

## Inspect roles and operating limits

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

The four engines open with two prefill and two decode roles. Startup
profiles 1P:3D, 2P:2D and 3P:1D so the controller can price changes in both
directions from the current processes. Prefill and decode sweeps cover
128 to 3,840 input tokens, with decode concurrency one and two and up to
128 output tokens. Use long inputs with short outputs to exercise prefill
growth, and longer outputs to exercise decode growth. The controller prices
each move from the profiles and resident work. The 4,096-token engine context
limit bounds input plus output.

The reference uses a 1-second TTFT budget and a 125-ms TPOT budget. Narwhal
samples engines every 100 ms and evaluates role changes every 250 ms, using
a 30-second demand window and three confirmations for ordinary moves. After
`verify`, fill one demand window with representative traffic before
assessing role changes.

Keep four engines for the default RTX 5090 setup. Changing engine count,
model, context length or memory fractions requires a matching template and
a fresh `up` and `verify` cycle. Export the installed reference to edit it:

```bash
python - <<'PY' > runs/dev-template.json
from importlib.resources import files
print(files('narwhal.dev').joinpath('reference-v1.json').read_text())
PY
narwhal dev init --instance runs/dev-custom --template runs/dev-template.json
```

## Diagnose startup and inference

Inspect the current run path printed by `status`:

| File | Contents |
| --- | --- |
| `engine-*/startup.log` | Model loading, cache allocation and engine requests. |
| `profile-*.log` | Probe failures and profile fit errors. |
| `router.log` | Router startup and request errors. |
| `journal.jsonl` | Admission, placement and controller decisions. |
| `verify-*/preflight.log` | Runtime, profile and directed transfer checks. |
| `*-memory.jsonl` | Whole-device VRAM samples. |
| `teardown.json` | Stopped process groups and cleanup errors. |

Free conflicting ports or select another `--port-base` in a new instance.
For a VRAM reserve failure, stop other GPU workloads before retrying. For
a model or plugin hash mismatch, restore the pinned files above. After
repairing a startup failure, run `down`, `up` and `verify` for that instance.

## Stop or restart

```bash
narwhal dev down
narwhal dev status
```

`down` waits for the recorded process groups to exit before reporting
`stopped`, escalating owned workers that survive SIGTERM to SIGKILL and
preserving the instance logs. If a leader exited before teardown could
establish worker ownership, it reports surviving group members as
`degraded`. Inspect the PIDs in `teardown.json`, stop workers confirmed to
belong to this instance, then repeat `down`.

Run `up` and `verify` again after changing the runtime or restarting an
engine so profiles and transfer checks bind to the new processes.
