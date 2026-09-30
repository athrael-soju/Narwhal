# Install the Narwhal dev CUDA runtime

Use an Ubuntu shell, native or under WSL2. The template installs
Qwen3.5-0.8B GGUF and pins the Python runtime and the GGUF plugin. The plugin
wheel targets Linux x86-64. The NVIDIA GPU and driver must support the CUDA
runtime of the pinned packages.

Install Narwhal and the pinned packages from a checkout on the Linux filesystem:

```bash
python3.12 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install .
python -m pip install 'vllm==0.29.0' 'torch==2.13.0' \
  'transformers==5.17.0' 'nixl==1.4.1' 'nixl-cu13==1.4.1'
python -m pip install \
  'https://github.com/vllm-project/vllm-gguf-plugin/releases/download/v0.0.5/vllm_gguf_plugin-0.0.5-cp310-abi3-manylinux_2_28_x86_64.whl'
```

Overwrite the wheel's Python files with the pinned GGUF plugin sources:

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

`narwhal dev init` checks installed package versions and plugin hashes against
the selected template. Reapply the pinned sources after every plugin wheel
reinstall.

Download the model and tokenizer into the Hugging Face cache:

```bash
hf download unsloth/Qwen3.5-0.8B-GGUF \
  --revision e524882462b3f2a9fe83be967c654c4322abb2f6 \
  Qwen3.5-0.8B-Q4_K_M.gguf
hf download Qwen/Qwen3.5-0.8B \
  --revision 2fc06364715b967f1860aea9cf38778875588b17 \
  --include '*.json' '*.txt' '*.jinja'
```

The template reads these revisions from the standard Hugging Face cache by
default. For a cache in another location, pass these options to
`narwhal dev init`:

| Option | Value |
| --- | --- |
| `--model` | The GGUF file |
| `--model-dir` | The tokenizer directory |
