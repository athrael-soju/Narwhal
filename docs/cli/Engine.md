# `narwhal-engine`

Prepare and run vLLM engines from the existing `narwhal.engine-launch` record. The `native` backend runs the checked Python environment on Linux or WSL2; `container` retains the existing Docker launch path. Both use the same model, GPU allocation, ports, NIXL connector and runtime argument checks.

The native path requires `NARWHAL_MODEL_REVISION` alongside the launch environment produced during deployment. A local GGUF file can be selected with `NARWHAL_MODEL_PATH`; preparation records its SHA-256. Each run directory is immutable. A fresh start needs a fresh directory.

```bash
narwhal-engine prepare --backend native --out runs/engine-1
narwhal-engine check --run runs/engine-1
narwhal-engine start-shared --backend native --run runs/engine-1 --run runs/engine-2
narwhal-engine stop-native --run runs/engine-1
```

`start-shared` checks every selected role, port and GPU budget before launching sequentially. It records the Linux PID, boot ID and process start tick, vLLM version and `/metrics` process start, model revision, arguments and GPU memory. `stop-native` signals only the recorded process group when its identity still matches. Native attestation and the installed `narwhal dev` workflow remain milestone gates.

| Option | Default | Purpose |
| --- | --- | --- |
| `--backend` | `"container"` | Select the native or container backend for preparation and shared startup. |
| `--out` | required | Create a fresh launch directory during preparation. |
| `--run` | required | Select an existing launch directory; repeat for shared startup. |
| `--ready-seconds` | `180` | Time allowed for each engine to pass health and identity checks. |
