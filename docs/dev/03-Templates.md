# Tune the template and memory allocation

A template describes what an instance runs: the model and runtime, the
context length, the profiling sweep, how GPU memory is divided, how much
memory to keep free, and the TTFT and TPOT latency budgets.

Memory allocation is the setting changed most often. Each engine gets a
fraction of total VRAM for model weights, runtime overhead and KV cache. The
device allowance caps the combined use of all engines.

| Setting                         | Installed template  | Template field                      | `narwhal dev init` flag    |
| ------------------------------- | ------------------- | ----------------------------------- | -------------------------- |
| GPU                             | Single detected GPU | —                                   | `--gpu` with a GPU UUID    |
| Engines                         | 2                   | `allocation.engine_count`           | `--engine-count`           |
| vLLM memory fraction per engine | 0.35 of total VRAM  | `allocation.gpu_memory_utilization` | `--gpu-memory-utilization` |
| Startup allowance for the card  | 0.8 of total VRAM   | `allocation.device_allowance`       | `--device-allowance`       |
| Free-memory reserve             | 512 MiB             | `gpu.reserve_mib`                   | —                          |
| TTFT budget                     | 5 s                 | `slo.ttft_s`                        | —                          |
| TPOT budget                     | 500 ms              | `slo.tpot_s`                        | —                          |

Two more fields, `gpu.product` and `gpu.minimum_total_mib`, restrict a
template to a specific card. The installed template leaves them unset. The
[RTX 5090 reference](08-RTX-5090-Reference.md) sets both.

`init` also checks that the GPU has at least
`device_allowance × total VRAM + gpu.reserve_mib` free. On an 8 GiB card with
the installed template values, that is about 6.9 GiB, most of the card. If `init` reports
insufficient free memory, stop other processes that use the GPU.

## Tuning an allocation

1. Check how much memory is free with the `nvidia-smi` query in
   [Prepare Ubuntu or WSL2](01-Prepare-Host.md).
2. Initialize a fresh instance with the `--gpu-memory-utilization` and
   `--device-allowance` values to try.
3. Measure TTFT and TPOT on that card.
4. Set `slo.ttft_s` and `slo.tpot_s` from the measured values in a
   [custom template](#building-a-custom-template).

## Building a custom template

Export the installed template:

```bash
mkdir -p runs
python - <<'PYTHON' > runs/small-cuda-template.json
from importlib.resources import files
print(files('narwhal.dev').joinpath('small-cuda-v1.json').read_text())
PYTHON
```

Edit the file, then initialize a new instance from it. Templates apply when
an instance is created, so use a fresh instance directory each time:

```bash
narwhal dev init --template runs/small-cuda-template.json --instance runs/dev-custom
```
