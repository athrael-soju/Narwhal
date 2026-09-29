# Prepare Ubuntu or WSL2

| Host          | NVIDIA driver                                                                              | Command shell            |
| ------------- | ------------------------------------------------------------------------------------------ | ------------------------ |
| Native Ubuntu | Ubuntu driver compatible with the selected CUDA runtime                                    | Ubuntu shell             |
| WSL2          | Windows driver from [NVIDIA's CUDA on WSL guide](https://docs.nvidia.com/cuda/wsl-user-guide/) | Ubuntu shell inside WSL2 |

Place the checkout, model, virtual environment and instance directory on the
Linux filesystem.

Inspect the GPU and IPv4 interfaces:

```bash
nvidia_smi=$(command -v nvidia-smi || printf '%s' /usr/lib/wsl/lib/nvidia-smi)
"$nvidia_smi" --query-gpu=name,uuid,memory.total,memory.used,driver_version --format=csv
ip -brief -4 address
```

Choose an interface with one IPv4 address for NIXL/UCX. Pass its name to
`narwhal dev init --interface`.
