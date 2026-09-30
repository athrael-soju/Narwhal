# Install the CUDA runtime and model

The installed template pins exact versions of vLLM, Torch, NIXL,
Transformers, and the GGUF loader, and specific revisions of the
Qwen3.5-0.8B GGUF model and its tokenizer. `narwhal dev init` checks each of
them, so install exactly the versions listed here.

Requirements:

- x86-64 machine running native Ubuntu or Ubuntu under WSL2
- Python 3.12
- NVIDIA GPU whose driver supports the pinned CUDA runtime

Run all commands from the root of a Narwhal checkout on the Linux
filesystem (under WSL2, outside `/mnt/c`).

## Install the Python packages

```bash
python3.12 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install .
python -m pip install 'vllm==0.29.0' 'torch==2.13.0' \
  'transformers==5.17.0' 'nixl==1.4.1' 'nixl-cu13==1.4.1'
python -m pip install \
  'https://github.com/vllm-project/vllm-gguf-plugin/releases/download/v0.0.5/vllm_gguf_plugin-0.0.5-cp310-abi3-manylinux_2_28_x86_64.whl'
```

## Apply the pinned GGUF loader sources

The template expects the loader sources at plugin commit `d4c1f0d`, which
differ from the ones in the wheel. Clone the plugin at that commit and copy
its Python files over the installed package:

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

`narwhal dev init` hashes these files and compares them with the template.
Reinstalling the wheel restores the original files. Re-run the copy script
afterward, or `narwhal dev init` and `narwhal dev up` refuse to continue
because the hash check fails.

## Download the model and tokenizer

Download the quantized weights and the tokenizer and config files (JSON, text,
and Jinja only) from the original model repository:

```bash
hf download unsloth/Qwen3.5-0.8B-GGUF \
  --revision e524882462b3f2a9fe83be967c654c4322abb2f6 \
  Qwen3.5-0.8B-Q4_K_M.gguf
hf download Qwen/Qwen3.5-0.8B \
  --revision 2fc06364715b967f1860aea9cf38778875588b17 \
  --include '*.json' '*.txt' '*.jinja'
```

If both stay in the default Hugging Face cache (`~/.cache/huggingface/hub`),
`narwhal dev init` finds them without extra flags. It reads only that path
and ignores `HF_HOME`. For any other location, pass the GGUF file with
`--model` and the tokenizer directory with `--model-dir`.
