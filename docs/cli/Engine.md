# `narwhal-engine`

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

`start-shared` checks every selected role, port and GPU budget before launching sequentially. It records the Linux PID, boot ID and process start tick, vLLM version and `/metrics` process start, model revision, arguments and GPU memory. It checks observed fleet GPU use against `shared_device.device_allowance` after each start. `native-capture` uses Narwhal's model, cache, NIXL and handshake checks, then writes a process-bound attestation. Set `NARWHAL_NODE_<n>_ATTESTATION_URL` and run `python -m narwhal.deployment.attestation_contract serve --run runs/engine-<n>` to serve it. `stop-native` signals only the recorded process group when its identity still matches.

When starting engines through Windows OpenSSH, keep a WSL terminal open for
the fleet's lifetime. Closing the final `wsl.exe` session can stop the WSL
instance and its engine processes.

| Option | Default | Purpose |
| --- | --- | --- |
| `--backend` | `"container"` | Select the native or container backend for preparation and shared startup. |
| `--out` | required | Create a fresh launch directory during preparation. |
| `--run` | required | Select an existing launch directory; repeat for shared startup. |
| `--ready-seconds` | `180` | Time allowed for each engine to pass health and identity checks. |
