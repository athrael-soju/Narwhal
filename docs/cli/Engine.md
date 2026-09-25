# `narwhal-engine`

`narwhal-engine --version` prints the distribution name and version from the executable's Python environment, then exits with status 0. [Installation](../Install-from-PyPI.md) covers version reporting from a source checkout.

Prepare and run vLLM engines from the existing `narwhal.engine-launch` record. The `native` backend runs the checked Python environment on Linux or WSL2; `container` retains the existing Docker launch path. Both use the same model, GPU allocation, ports, NIXL connector and runtime argument checks.

The native path requires `NARWHAL_MODEL_REVISION` alongside the launch environment produced during deployment. A local GGUF file can be selected with `NARWHAL_MODEL_PATH`; preparation records its SHA-256. Each run directory is immutable. A fresh start needs a fresh directory.

For GGUF, pin `vllm-gguf-plugin` in `runtime.expected_packages` and keep the model path inside its snapshot directory so the loader can find companion files. Supply the base model tokenizer and configuration through `--tokenizer` and `--hf-config-path` in `runtime.extra_args`.

## Actions

| Action | Backend | Inputs and operation |
| --- | --- | --- |
| `prepare` | container, native | Read `NARWHAL_ENGINE_LAUNCH_CONFIG` and the [deployment environment](../deploy/02-Install.md), verify model and hook hashes, then write a fresh launch directory with `launch.json` and the backend environment. |
| `check` | container, native | Read the prepared plan, check the pinned packages, model, tokenizer and NIXL connector, then bind `checked.json` to that plan. Repeated checks append their attempts to the backend check log. |
| `measure-cache` | container | Start a temporary sizing container from a checked, unused plan, write `cache-layout.json`, then remove the completed sizing container. |
| `model-dimensions` | container | Inspect the model through the checked runtime and write `model-dimensions.json`. |
| `handshake-policy` | container, native | Inspect the installed NIXL worker against the checked connector settings and write `handshake-policy.json`. |
| `start` | container | Start one serving container from a checked plan and record `container.id`. Verify its HTTP readiness after launch. |
| `capture-cache` | container | Read the running container recorded in a checked launch directory and retain its live cache pages in `cache-layout.json`. |
| `cache-registration` | container, native | Resolve block grouping from the checked runtime plus either a serving startup log or captured runtime layout; write `cache-registration.json`. |
| `start-shared` | container, native | Validate two to eight checked plans sharing one GPU, start them sequentially, and retain readiness, identity and memory readings in each `shared-start.json`. |
| `stop-native` | native | Validate recorded boot ID and process start ticks, then stop the owned process groups and write `native-stop.json`. |

Preparation requires a fresh `--out`; subsequent actions use `--run`. Each inspection output is retained in its launch directory, so select a fresh prepared and checked directory when recapturing it.

| Option | Default | Purpose |
| --- | --- | --- |
| `--backend` | `container` | Select `container` or `native` for `prepare` and `start-shared`; shared startup requires every selected plan to use that backend. Other actions read the plan's backend. |
| `--out` | required for `prepare` | Create a fresh launch directory. |
| `--run` | required after preparation | Select an existing launch directory; repeat two to eight times for `start-shared`. |
| `--ready-seconds` | `180` | Positive integer seconds allowed per engine's readiness and identity checks during `start-shared`. |
| `--startup-log` | choose one registration source | Serving log with one resolved KV layout, used by `cache-registration`; exclusive with `--runtime-layout`. |
| `--runtime-layout` | choose one registration source | Captured runtime cache-layout JSON used by `cache-registration`; exclusive with `--startup-log`. |

## Native shared startup

```bash
narwhal-engine prepare --backend native --out runs/engine-1
narwhal-engine check --run runs/engine-1
narwhal-engine start-shared --backend native --run runs/engine-1 --run runs/engine-2
python -m narwhal.deployment.attestation_contract native-capture --run runs/engine-1
narwhal-engine stop-native --run runs/engine-1
```

`start-shared` checks every selected role, port and GPU budget before launching sequentially. All selected plans must share their group, GPU UUID and device allowance, with unique roles and ports. Each engine's `gpu_memory_utilization` is a fraction of total device memory; the decimal sum of those fractions must be at most `shared_device.device_allowance`. The selected CUDA device must resolve to `shared_device.gpu_uuid`; numeric ordinals and UUID prefixes resolve inside the checked runtime with its recorded environment.

Both backends record the first prelaunch GPU reading as their baseline and compare the increase in whole-device memory use after each engine passes readiness and identity checks with `shared_device.device_allowance` times total device memory. Equality passes. Existing allocations consume the free headroom checked before each launch; allocations added or released by other processes during startup also affect the observed increase. Each ready engine's `shared-start.json` retains the baseline, before/after readings, per-engine and aggregate increases, and allowance in MiB.

The container backend records each container ID, Linux PID, image ID and serving arguments. A failed invocation removes every container it created in reverse launch order and updates their `shared-start.json` records to `failed`, retaining the startup cause and each removal outcome in `cleanup_status`. A removal failure retains `cleanup_error` and appears in the command error; inspect the recorded container ID before recovery. Container IDs and launch evidence remain in the run directories.

The native backend records the Linux PID, boot ID and process start tick, vLLM version and `/metrics` process start, model revision and arguments. A failed invocation terminates its current process and stops previously ready process groups using their recorded identities. The failing engine's `shared-start.json` retains its startup cause, cleanup errors and post-cleanup GPU reading; `native-stop.json` records successful stops of previously ready engines.

Native port checks bind the HTTP endpoint from `launch.json` and the NIXL address from `engine.env`, preserving vLLM's kernel IPv6 default and NIXL's dual-stack bind. Supply `NARWHAL_NODE_<n>_ATTESTATION_URL` at preparation or shared startup to check the sidecar's configured address through Uvicorn's event loop; the startup environment takes precedence over the prepared URL. `narwhal dev` supplies the URL from the instance fleet. Each process binds again at startup to detect an address claimed after the check.

`native-capture` applies the recorded engine environment to Narwhal's model, cache, NIXL and handshake checks, then writes a process-bound attestation. Set `NARWHAL_NODE_<n>_ATTESTATION_URL` and run `python -m narwhal.deployment.attestation_contract serve --run runs/engine-<n>` to serve it. `stop-native` verifies the recorded process identity and waits for owned workers through group-leader exit, escalating survivors to SIGKILL.

When starting engines through Windows OpenSSH, keep a WSL terminal open for
the fleet's lifetime. Closing the final `wsl.exe` session can stop the WSL
instance and its engine processes.
