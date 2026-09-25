# Install the Narwhal dev CUDA runtime

Run these commands in an Ubuntu shell, either on native Ubuntu or Ubuntu under
WSL2. The installed template uses Qwen3.5-0.8B GGUF and pins the Python
runtime and GGUF loader. The wheel below targets Linux x86-64; the selected
NVIDIA GPU and driver must support that CUDA runtime.

From a Narwhal checkout on the Linux filesystem:

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

`narwhal dev init` checks the installed versions and plugin hashes against
the selected template. Reapply the pinned sources after reinstalling the
plugin wheel.

Download the model and tokenizer into the Hugging Face cache:

```bash
hf download unsloth/Qwen3.5-0.8B-GGUF \
  --revision e524882462b3f2a9fe83be967c654c4322abb2f6 \
  Qwen3.5-0.8B-Q4_K_M.gguf
hf download Qwen/Qwen3.5-0.8B \
  --revision 2fc06364715b967f1860aea9cf38778875588b17 \
  --include '*.json' '*.txt' '*.jinja'
```

The template resolves those revisions in the default cache. For another
cache location, pass the GGUF file with `--model` and the tokenizer directory
with `--model-dir` when initializing the instance.
