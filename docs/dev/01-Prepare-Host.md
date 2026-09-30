# Prepare Ubuntu or WSL2

Every command in these guides runs in an Ubuntu shell, on a native Ubuntu
install or inside WSL2. The NVIDIA driver source differs between the two.

On native Ubuntu, install an Ubuntu NVIDIA driver that supports the CUDA
runtime you plan to use. Under WSL2, GPU access comes from the Windows
driver. Follow [NVIDIA's CUDA on WSL guide](https://docs.nvidia.com/cuda/wsl-user-guide/)
and install no driver inside Ubuntu.

Keep the Narwhal checkout, the model, the virtual environment, and the
instance directory on the Linux filesystem. Under WSL2, place them in your
Linux home directory, outside `/mnt/c`. Reads across the Windows boundary are
slow, especially when loading a model.

## Check the GPU and network

```bash
nvidia_smi=$(command -v nvidia-smi || printf '%s' /usr/lib/wsl/lib/nvidia-smi)
"$nvidia_smi" --query-gpu=name,uuid,memory.total,memory.used,driver_version --format=csv
ip -brief -4 address
```

Some WSL2 installs leave `nvidia-smi` off the `PATH`, so the first line
falls back to the copy WSL keeps in `/usr/lib/wsl/lib`. Record the memory
figures for allocation tuning, and the UUID on a machine with multiple GPUs.

From the `ip` output, pick an interface with exactly one IPv4 address. NIXL
and UCX use it to move KV cache between engines, and `narwhal dev init`
rejects an interface with no IPv4 address or more than one. Pass the
interface name to `narwhal dev init --interface`. If you omit the flag, `init`
uses `eth0`.
