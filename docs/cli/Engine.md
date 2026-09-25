# `narwhal-engine`

`narwhal-engine --version` prints the distribution name and version from the executable's Python environment, then exits with status 0. [Installation](../Install-from-PyPI.md) covers version reporting from a source checkout.

Prepare and run vLLM engines from the existing `narwhal.engine-launch` record. The `native` backend runs the checked Python environment on Linux or WSL2; `container` retains the existing Docker launch path. Both use the same model, GPU allocation, ports, NIXL connector and runtime argument checks.

The native path requires `NARWHAL_MODEL_REVISION` alongside the launch environment produced during deployment. A local GGUF file can be selected with `NARWHAL_MODEL_PATH`; preparation records its SHA-256. Each run directory is immutable. A fresh start needs a fresh directory.

For GGUF, pin `vllm-gguf-plugin` in `runtime.expected_packages` and keep the model path inside its snapshot directory so the loader can find companion files. Supply the base model tokenizer and configuration through `--tokenizer` and `--hf-config-path` in `runtime.extra_args`.

```bash
narwhal-engine prepare --backend native --out runs/engine-1
narwhal-engine check --run runs/engine-1
narwhal-engine start-shared --backend native --run runs/engine-1 --run runs/engine-2
python -m narwhal.deployment.attestation_contract native-capture --run runs/engine-1
narwhal-engine stop-native --run runs/engine-1
```

`start-shared` checks every selected role, port and GPU budget before launching sequentially. The selected CUDA device must resolve to `shared_device.gpu_uuid`; numeric ordinals and UUID prefixes resolve inside the checked runtime with its recorded environment.

Both backends record the first prelaunch GPU reading as their baseline and compare the increase in whole-device memory use after each engine passes readiness and identity checks with `shared_device.device_allowance` times total device memory. Equality passes. Existing allocations consume the free headroom checked before each launch; allocations added or released by other processes during startup also affect the observed increase. Each ready engine's `shared-start.json` retains the baseline, before/after readings, per-engine and aggregate increases, and allowance in MiB.

The container backend records each container ID, Linux PID, image ID and serving arguments. A failed invocation removes every container it created in reverse launch order and updates their `shared-start.json` records to `failed`, retaining the startup cause and each removal outcome in `cleanup_status`. A removal failure retains `cleanup_error` and appears in the command error; inspect the recorded container ID before recovery. Container IDs and launch evidence remain in the run directories.

The native backend records the Linux PID, boot ID and process start tick, vLLM version and `/metrics` process start, model revision and arguments. A failed invocation terminates its current process and stops previously ready process groups using their recorded identities. The failing engine's `shared-start.json` retains its startup cause, cleanup errors and post-cleanup GPU reading; `native-stop.json` records successful stops of previously ready engines.

Native port checks bind the HTTP endpoint from `launch.json` and the NIXL address from `engine.env`, preserving vLLM's kernel IPv6 default and NIXL's dual-stack bind. Supply `NARWHAL_NODE_<n>_ATTESTATION_URL` at preparation or shared startup to check the sidecar's configured address through Uvicorn's event loop; the startup environment takes precedence over the prepared URL. `narwhal-dev` supplies the URL from the instance fleet. Each process binds again at startup to detect an address claimed after the check.

`native-capture` applies the recorded engine environment to Narwhal's model, cache, NIXL and handshake checks, then writes a process-bound attestation. Set `NARWHAL_NODE_<n>_ATTESTATION_URL` and run `python -m narwhal.deployment.attestation_contract serve --run runs/engine-<n>` to serve it. `stop-native` verifies the recorded process identity and waits for owned workers through group-leader exit, escalating survivors to SIGKILL.

When starting engines through Windows OpenSSH, keep a WSL terminal open for
the fleet's lifetime. Closing the final `wsl.exe` session can stop the WSL
instance and its engine processes.

| Option | Default | Purpose |
| --- | --- | --- |
| `--backend` | `"container"` | Select the native or container backend for preparation and shared startup. |
| `--out` | required | Create a fresh launch directory during preparation. |
| `--run` | required | Select an existing launch directory; repeat for shared startup. |
| `--ready-seconds` | `180` | Time allowed for each engine to pass health and identity checks. |
