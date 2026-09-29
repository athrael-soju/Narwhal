# Tune the template and memory allocation

A template sets the model, runtime, context length, profiling sweep, memory
allocation, reserve and latency budgets. Each engine's memory fraction covers
model weights, runtime overhead and KV cache.

| Setting                          | Installed template  | Template field                      | `narwhal dev init` flag    |
| -------------------------------- | ------------------- | ----------------------------------- | -------------------------- |
| GPU                              | Single discovered GPU |                                   | `--gpu` with a GPU UUID    |
| Engine count                     | 2                   | `allocation.engine_count`           | `--engine-count`           |
| vLLM memory fraction per engine  | 0.35 of total VRAM  | `allocation.gpu_memory_utilization` | `--gpu-memory-utilization` |
| Whole-device startup allowance   | 0.8 of total VRAM   | `allocation.device_allowance`       | `--device-allowance`       |
| Free-memory reserve              | 512 MiB             | `gpu.reserve_mib`                   |                            |
| TTFT budget                      | 5 s                 | `slo.ttft_s`                        |                            |
| TPOT budget                      | 500 ms              | `slo.tpot_s`                        |                            |
| Pinned GPU product               |                     | `gpu.product`                       |                            |
| Minimum total VRAM               |                     | `gpu.minimum_total_mib`             |                            |

`init` requires free VRAM of at least:

```text
device_allowance × total VRAM + gpu.reserve_mib
```

## Tune an allocation

1. Check the card's free memory with the query in
   [Prepare Ubuntu or WSL2](01-Prepare-Host.md).
2. Initialize a fresh instance with `--gpu-memory-utilization` and
   `--device-allowance`.
3. Measure TTFT and TPOT on the selected card.
4. Set `slo.ttft_s` and `slo.tpot_s` in a custom template from those
   measurements.

## Build a custom template

Export the installed template:

```bash
mkdir -p runs
python - <<'PYTHON' > runs/small-cuda-template.json
from importlib.resources import files
print(files('narwhal.dev').joinpath('small-cuda-v1.json').read_text())
PYTHON
```

Initialize a fresh instance from the edited file:

```bash
narwhal dev init --template runs/small-cuda-template.json --instance runs/dev-custom
```
