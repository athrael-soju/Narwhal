# Prepare Ubuntu or WSL2

Every command in these guides runs in an Ubuntu shell, either on a native
Ubuntu install or inside WSL2. The main difference between the two is where
the NVIDIA driver comes from.

On native Ubuntu, install an Ubuntu NVIDIA driver that supports the CUDA
runtime you plan to use. Under WSL2, GPU access comes from the Windows
driver instead, so follow [NVIDIA's CUDA on WSL guide](https://docs.nvidia.com/cuda/wsl-user-guide/)
rather than installing a driver inside Ubuntu.

Keep the Narwhal checkout, the model, the virtual environment, and the
instance directory on the Linux filesystem. Under WSL2 that means somewhere
in your Linux home directory, not under `/mnt/c`. Reading files across the
Windows boundary is slow, and loading a model from there is noticeably
slower.

## Check the GPU and network

```bash
nvidia_smi=$(command -v nvidia-smi || printf '%s' /usr/lib/wsl/lib/nvidia-smi)
"$nvidia_smi" --query-gpu=name,uuid,memory.total,memory.used,driver_version --format=csv
ip -brief -4 address
```

`nvidia-smi` often isn't on the `PATH` under WSL2, so the first line falls
back to the copy WSL keeps in `/usr/lib/wsl/lib`. Keep the GPU output handy:
you'll want the total and used memory figures if you end up tuning the
allocation, and the UUID if the machine has more than one GPU.

From the `ip` output, pick an interface with exactly one IPv4 address. NIXL
and UCX use it to move KV cache between engines, and `narwhal dev init`
rejects an interface with no IPv4 address or more than one. You'll pass the
interface name to `narwhal dev init --interface`. Without that flag, `init`
uses `eth0`.
