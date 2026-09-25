# Set up the WSL2 GPU runtime

Use Ubuntu under WSL2, Python 3.12 and an RTX 5090 with 32 GB VRAM.
Check GPU access from the WSL2 shell:

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
  Qwen3.5-0.8B-Q4_K_M.gguf mmproj-F16.gguf
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

The router listens on `127.0.0.1:18000`. Engine HTTP ports start at 18101,
attestation ports at 18201, and NIXL side-channel ports at 5701. Select
another port layout with `dev init --port-base`, or another instance with
`--instance` on each command.

```bash
curl http://127.0.0.1:18000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.5-0.8B-GGUF-Q4_K_M","messages":[{"role":"user","content":"Reply with only the number: 2 + 3 = ?"}],"temperature":0,"max_tokens":32}'
```

Expect the response content `5`. Inspect `runs/dev/run-*/router.log` and
`engine-*/startup.log` when startup or inference fails. The same run
directory contains profiles, transfer evidence, metrics and VRAM samples.

## Stop or restart

```bash
narwhal dev down
narwhal dev status
```

Expect `stopped`. `down` signals the process groups recorded for this
instance and preserves its logs. Run `up` and `verify` again after changing
the runtime or restarting an engine so profiles and transfer checks bind
to the new processes.
