# Narwhal Dev native reference

The installed distribution contains `narwhal.dev/reference-v1.json`. It describes three colocated native vLLM engines on one NVIDIA GeForce RTX 5090. The model is `unsloth/Qwen3.5-0.8B-GGUF` at revision `e524882462b3f2a9fe83be967c654c4322abb2f6`, using `Qwen3.5-0.8B-Q4_K_M.gguf` with SHA-256 `bd258782e35f7f458f8aced1adc053e6e92e89bc735ba3be89d38a06121dc517`. The runtime versions are vLLM 0.29.0, NIXL 1.4.1 and `vllm-gguf-plugin` 0.0.5. These values come from the RTX 5090 WSL2 run; #41 still owns the final installed wheel hashes and fresh topology qualification.

Each engine has a 0.1 GPU memory utilization budget. The shared allocation allows 0.4 of the device, with a separate 2048 MiB free VRAM reserve. The template holds the engine count, ports, budgets and opening roles; it does not contain workload phases or requested role changes. Narwhal's controller makes role changes from measured profiles and live demand.

`narwhal.dev.template.materialize` accepts an instance directory, a local model metadata directory containing `config.json`, the GGUF path, and a Linux fabric interface name. It discovers the GPU UUID and interface address, checks the model digest, GPU identity and free VRAM, installed package versions, NIXL connector import and port availability, then writes private `fleet.json`, `engine-launch.json`, `instance.json` and canonical Prometheus target documents. The generated fleet and launch records are checked by the same validators as other Narwhal deployments. An existing instance directory is never overwritten.

```python
from pathlib import Path
from narwhal.dev.template import materialize

materialize(
    Path.home() / ".local/share/narwhal/dev/example",
    model_dir=Path("/path/to/model-metadata"),
    model_path=Path("/path/to/Qwen3.5-0.8B-Q4_K_M.gguf"),
    fabric_interface="eth0",
)
```

The instance is consumed by the `narwhal dev` CLI in #68. The default template is specific to the qualified GPU, model and runtime. A different count or hardware reference requires its own measured allocation and template revision.
