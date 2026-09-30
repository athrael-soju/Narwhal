# Install the CUDA runtime and model

The installed template pins exact versions of vLLM, Torch, NIXL,
Transformers, and the GGUF loader, as well as specific revisions of the
Qwen3.5-0.8B GGUF model and its tokenizer. `narwhal dev init` checks all of
them, so install exactly what's listed here.

You'll need an x86-64 machine running native Ubuntu or Ubuntu under WSL2,
Python 3.12, and an NVIDIA GPU whose driver supports the pinned CUDA
runtime. Run everything below from the root of a Narwhal checkout on the
Linux filesystem.

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

The last command installs the GGUF plugin from its release wheel. The next
step replaces part of it.

## Apply the pinned GGUF loader sources

The template expects the loader code from a particular plugin commit, not
the code that ships in the wheel. Clone the plugin at that commit and copy
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
Reinstalling the wheel puts the original files back, so if you ever
reinstall it, run the copy step again or `init` and `up` will refuse to
continue.

## Download the model and tokenizer

```bash
hf download unsloth/Qwen3.5-0.8B-GGUF \
  --revision e524882462b3f2a9fe83be967c654c4322abb2f6 \
  Qwen3.5-0.8B-Q4_K_M.gguf
hf download Qwen/Qwen3.5-0.8B \
  --revision 2fc06364715b967f1860aea9cf38778875588b17 \
  --include '*.json' '*.txt' '*.jinja'
```

The first command fetches the quantized weights. The second pulls only the
tokenizer and config files from the original model repository.

If you leave both in the default Hugging Face cache
(`~/.cache/huggingface/hub`), `narwhal dev init` finds them without any
extra flags. It looks only there, even if you've set `HF_HOME`. If you put
them somewhere else, pass the GGUF file with `--model` and the tokenizer
directory with `--model-dir`.
