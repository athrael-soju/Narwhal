---
description: Options, exit codes and run directories for narwhal dev on one workstation.
---

# `narwhal dev`

`narwhal dev` runs a development fleet on one workstation, on Linux natively or under WSL2. With `setup`, the fleet holds llama.cpp engines and a router, on a GPU or on the CPU, and [the quick start](../dev/Quick-Start.md) covers that path. With `init`, the fleet holds native vLLM, SGLang or TensorRT-LLM engines, their attestation sidecars and a router. The engines share one NVIDIA CUDA GPU, or each runs [on its own GPU](#one-gpu-per-engine). To set up the host for those, follow [vLLM, SGLang and TensorRT-LLM engines](../Dev-Runtime.md). A template pins the model, the runtime, the memory budget and the ports. `init` takes one of four installed templates, and `setup` one of the two [llama.cpp templates](../dev/Llama-Cpp-Engines.md#templates):

| Template | Engine | Engines | Model input |
| --- | --- | --- | --- |
| `small-cuda-v1.json`, the default | vLLM | 1 prefill, 1 decode | GGUF file |
| [`small-cuda-sglang-v1.json`](../Dev-Runtime.md#running-sglang-engines) | SGLang with Mooncake | 1 prefill, 1 decode | Checkpoint directory |
| [`small-cuda-trtllm-v1.json`](../Dev-Runtime.md#running-tensorrt-llm-engines) | TensorRT-LLM with UCX | 1 prefill, 1 decode | Checkpoint directory |
| [`reference-v1.json`](../dev/RTX-5090-Reference.md), for an RTX 5090 | vLLM | 2 prefill, 2 decode | GGUF file |
| [`direct-ernie45-03b-v1.json`](../dev/Llama-Cpp-Engines.md#templates), the default of `setup` | llama.cpp | 2 prefill, 2 decode | GGUF file that `setup` downloads |
| [`direct-qwen35-08b-v1.json`](../dev/Llama-Cpp-Engines.md#templates) | llama.cpp | 2 prefill, 2 decode | GGUF file that `setup` downloads |

## Lifecycle

Run the subcommands with the Python environment that ran `setup` or `init`:

```bash
narwhal dev setup
narwhal dev up
narwhal dev verify
narwhal dev ask "What is the capital of France?"
narwhal dev watch
narwhal dev demo
narwhal dev status
narwhal dev down
```

For vLLM, SGLang or TensorRT-LLM engines, run `narwhal dev init` in place of `narwhal dev setup`.

| Subcommand | Operation | Reports |
| --- | --- | --- |
| `setup` | Downloads the engine build and model file that a template pins into `assets` under the instance directory, checks their SHA-256 values, and runs the `init` checks for a template that starts its engines directly. On a stopped instance, an option that differs from the instance changes it in place and keeps the downloaded files. | `initialized`, `reconfigured`, or `reused` |
| `init` | Checks the model files, runtime packages, the GGUF plugin hashes that a vLLM template pins, GPU, network interface and ports, then writes `template.json`, `instance.json`, `engine-launch.json` and `fleet.json` to the instance directory. | `initialized`, or `reused` |
| `up` | Checks the model, tokenizer and GGUF plugin hashes, then prepares, checks and starts the engines with [`narwhal-engine`](Engine.md), captures their attestations, starts the attestation sidecars, profiles the engines and starts the router. For llama.cpp engines, starts each engine process, sends short requests to every engine at once, writes a [nominal profile](../telemetry/02-Profiles.md#nominal-profiles) for each engine from its replies, and starts the router. While another instance's fleet is up, `up` starts nothing and names that instance; `--alongside` starts the fleet beside it. After a failed or interrupted `up`, `up` [resumes](#resuming-an-interrupted-up) at the stage that did not pass. When the instance does not exist, or a `setup` option is given, `up` runs `setup` with those options first; an instance from `init` takes no `setup` option. | `launched` |
| `verify` | Records [KV evidence](Check.md#recording-and-verifying-kv-evidence) for every directed pair, sends a routed `2 + 3` request that must return `5`, saves router and engine metrics, and checks the router's `/ready`. For llama.cpp engines, sends one routed completion that must return text and checks `/ready`. | `ready` |
| `ask` | Takes one prompt as its argument, after `--` when the prompt starts with a hyphen. Sends it through the router with temperature 0, unless the template's request settings name a temperature, so the same prompt gets the same reply. Prints the reply, its time to first token, its tokens per second and the engine that served each leg. With `--repeat`, sends that many requests, of the given prompt or of the built-in prompts when none is given, and prints the time to first token and the time per output token at the median and the 95th percentile, the throughput and the count of failed requests. | `answered`, or `completed` |
| `watch` | Redraws, from the router's `/narwhal/state` and `/metrics`, each engine's role and requests in flight, the time to first token and the time per output token at the median and the 95th percentile, the throughput, each engine's role history, the role switches and the role controller's last decision. The router measures both times: time to first token from the request's arrival to the end of prefill, and time per output token as the mean interval between output tokens after prefill. They differ from the client's view that `ask` reports. Output tokens are counted when a request completes. It runs until interrupted and prints the view in place of a result document. With `--once` or `--format json` it takes two readings one interval apart and prints that frame as its result. | `observed` |
| `demo` | Starts the fleet first when it is not up, setting up the instance as `up` does. Then sends long replies, built-in prompts with 200 output tokens, for `--phase-seconds`, and long prompts of about 450 words with 2 output tokens for as long again, keeping `--concurrency` requests in flight. In a terminal the live view of `watch` runs meanwhile, with a line naming the phase, and closes a few seconds after the load ends. Prints the requests each phase sent and failed, the role switches and the final split, and leaves the fleet running. | `completed`, or `interrupted` after Ctrl+C |
| `status` | Reads the recorded processes, health routes and saved evidence. | A state from the next table |
| `down` | Sends SIGTERM to each recorded process group and SIGKILL to members still running 10 seconds later. With `--remove`, then deletes the instance directory with its downloads and logs. | `stopped`, or `removed` |

`status` reports one of these states:

| State | Condition |
| --- | --- |
| `starting` | `up` is running. |
| `launched` | Every engine, sidecar and router process is running, every engine and the router answer `/health`, and the router answers `/ready`. |
| `ready` | The `launched` conditions hold, and the KV evidence of the last `verify` still verifies against the running engines. |
| `degraded` | `problems` lists a failed condition, a surviving process, or a failed `verify`. |
| `stopped` | The last generation stopped, or a failed `up` left none of its processes running, and none of them survive. Or `up` has never run. |

`up` starts a new run on an instance that has never started or whose last generation `down` stopped, and resumes the run of a failed or interrupted `up`. On an instance whose fleet is up, it refuses. When the engines share a GPU and the template's `profile` sets neighbour traffic, as the vLLM templates do, `up` profiles every role split from 1 prefill engine to one less than the engine count with [`--colocated`](Profile.md#shared-gpu-neighbour-traffic), and merges the splits when there are several. Otherwise, as with the SGLang template or engines on their own GPUs, `up` profiles only the opening split. On SGLang, the [SGLang pair sweep](Profile.md#sglang-pairs) measures both roles of each engine.

Running `init` on an existing instance changes nothing. When the settings you supply match the saved instance, `init` reports `reused`. When a setting conflicts, `init` exits with code `2` and lists the conflicting options. To change a setting, run `init` with a new `--instance` directory.

To authenticate engine requests, export `NARWHAL_ENGINE_API_KEY` before `up` and keep it set for later commands. `up` passes it to the engines, and sets `engine.engine_api_key_env` in the fleet configuration so that the profiler, preflight and router send it.

A failed `verify` writes its reason, evidence directory and failure time to `verification_failure` in `lifecycle.json` and to `failure.json` in its evidence directory. `status` reports `degraded` with that `verification_failure` until a later `verify` succeeds or `down` stops every process.

## Output and exit codes

In text mode, stdout carries the result as one JSON document, and stderr carries preparation and profiling progress and error diagnostics. To save the result, redirect stdout, as in `narwhal dev up > result.json`.

| Outcome | Exit code |
| --- | :--: |
| `initialized`, `reconfigured`, `reused`, `starting`, `launched`, `ready`, `stopped`, `removed`, `observed`, `answered`, or `completed` with every request served | `0` |
| `degraded`, a failed `up`, `verify` or `down`, a failed `ask` request, or an `ask --repeat` or `demo` run with a failed request | `1` |
| Invalid `ask`, `watch` or `demo` arguments, a failed `setup` download or check, a failed `init` check, an unreadable instance or lifecycle record, a missing runtime package, or a Python environment other than the one that ran `setup` or `init` | `2` |

With `--format json`, the command prints a [versioned command result](../Command-Results.md), and the exit code follows its status. A `degraded` instance exits with code `3`, a failed step or routed check during `up` or `verify` and a failed single `ask` request exit with code `4`, and an `ask --repeat` run with a failed request exits with code `1`.

## Options

`--instance` and `--format` apply to every subcommand. `--model-name` and `--accelerator` apply to `setup`, `up` and `demo`, `--repeat` and `--max-tokens` to `ask`, `--concurrency` to `ask` and `demo`, `--phase-seconds` to `demo`, `--alongside` to `up`, `--remove` to `down`, `--interval` to `watch` and `demo`, `--once` to `watch`, `--engine-count` and `--port-base` to `setup`, `up`, `demo` and `init`, and the other options to `init`. An omitted `setup` or `init` option takes its value from the saved instance or the template. `narwhal --version` prints the installed distribution version.

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--instance` | `runs/dev` | Private instance directory. | |
| `--format` | `text` | Output format, either `text` or `json` for [versioned command results](../Command-Results.md). | |
| `--template` | `small-cuda-v1.json` | Template file with versioned model, tokenizer, runtime, profiling and memory settings. | |
| `--model` | The template's pinned file or snapshot in the Hugging Face cache | Model to serve, a GGUF file for a vLLM template or a checkpoint directory for an SGLang template. | Matches the template's checksums, and for a GGUF file its file name |
| `--model-dir` | The template's pinned tokenizer snapshot in the Hugging Face cache | Directory with the tokenizer files and `config.json`. | Matches the template's checksums |
| `--gpu` | The only GPU that `nvidia-smi` lists | Physical GPU UUID that every engine shares. | |
| `--gpus` | None | Physical GPU UUIDs separated by commas, one GPU per engine, in place of `--gpu`. Engine `n` runs on the `n`th GPU of the list. | Distinct UUIDs that start with `GPU-` and that `nvidia-smi` lists, at least `--engine-count` of them |
| `--alongside` | Off | Starts the fleet while another instance's fleet is up. Each fleet takes its own memory. | Flag |
| `--remove` | Off | Deletes the instance directory after `down` stops its fleet. | Flag |
| `--engine-count` | Template value, `2` in the small templates and `4` in the llama.cpp templates | Number of engines. | 2 to 8 |
| `--port-base` | Template ports | Base of the port layout below. | Every port within 1 to 65535 and distinct |
| `--engine-memory-mib` | Template value, `2867` in the small templates | Each engine's GPU memory in MiB, which `init` converts to a fraction of the GPU it finds. On SGLang it is an upper bound, since the engine applies the fraction to the memory free at its start. | A positive integer |
| `--device-memory-mib` | Template value, `7168` in the small vLLM template and `9728` in the small SGLang template | GPU memory in MiB that bounds the sum of the engines' memory and the memory growth during startup. | At least `--engine-count` times `--engine-memory-mib`, and at most the GPU's total |
| `--gpu-memory-utilization` | Template value, `0.1` in the reference template | Each engine's fraction of total GPU memory on vLLM, or of the GPU memory free at engine start on SGLang. Pass it with `--device-allowance` on a template that states MiB. | Above 0 and at most 1 |
| `--device-allowance` | Template value, `0.5` in the reference template | Fraction of total GPU memory that bounds the sum of engine fractions and the memory growth during startup. | At least `--engine-count` times `--gpu-memory-utilization`, and at most 1 |
| `--interface` | `eth0` | Network interface for KV transfer. | An interface with exactly one IPv4 address |
| `--engine-binary` | None | Engine server executable, for a template that starts its engines directly. | An executable file of the build the template pins |
| `--no-accelerator` | Off | Keeps the model on the CPU, for a template that starts its engines directly. | A flag with no value |
| `--accelerator` | `auto` | Engine build that `setup` downloads. `auto` selects by the host's devices. | `auto`, `none` for the CPU, or a build the template pins |
| `--interval` | `1.0` | Seconds between redraws of `watch` and `demo`. | A positive number |
| `--once` | Off | Makes `watch` print one frame as its result and exit. | A flag with no value |
| `--repeat` | None | Number of requests `ask` sends, of the given prompt or of the built-in prompts. | A positive integer |
| `--concurrency` | `1` for `ask`, `8` for `demo` | Requests `ask` keeps in flight with `--repeat`, or `demo` keeps in flight. | A positive integer |
| `--phase-seconds` | `30` | Seconds of long replies that `demo` sends, then of long prompts. | A positive number |
| `--max-tokens` | `128` | Output token limit of each `ask` request. | A positive integer |
| `--model-name` | The default template's model | Served name of the model `setup` prepares. | A model that an installed template starting its engines directly serves |

`--port-base P` puts the router at `P` and starts the engine HTTP, attestation and KV side-channel ranges at `P+1`, `P+101` and `P+201`, one port per engine. A template that starts its engines directly uses the router port and the engine HTTP range alone. The KV side channel is the NIXL side channel on vLLM and the bootstrap server on SGLang. The installed templates put the router at 18000 and start the ranges at 18101, 18201 and 5701. `init` and `up` require every port to be free on `127.0.0.1`.

`init` also checks the GPU:

| Check | Requirement |
| --- | --- |
| Free memory | Free VRAM covers the fleet's allowance plus the template's `gpu.reserve_mib`: about 7,680 MiB for the small vLLM template and about 10,240 MiB for the small SGLang template, and `--device-allowance` times total VRAM plus 2,048 MiB for the reference template. |
| GPU product | The GPU name matches the template's `gpu.product` when the template sets it. |
| Total memory | Total VRAM is at least the template's `gpu.minimum_total_mib` when the template sets it. |

### One GPU per engine

With `--gpus`, `init` gives engine `n` the `n`th GPU of the list and writes no `shared_device` group to `fleet.json`. It uses the first `--engine-count` GPUs of the list and records them in order as `gpu_uuids` in `instance.json`. Templates that start their engines directly, the llama.cpp templates, refuse `--gpus`.

The memory options then apply to each GPU, which holds one engine:

- `init` converts `--engine-memory-mib` and `--device-memory-mib` to fractions of the smallest of the engines' GPUs.
- `--device-memory-mib` must be at least `--engine-memory-mib`, and `--device-allowance` at least `--gpu-memory-utilization`.
- `init` requires each engine's GPU to have free memory for the engine's fraction plus `gpu.reserve_mib`, about 3,380 MiB for the small vLLM and SGLang templates and 6,660 MiB for the small TensorRT-LLM template. It also requires the engines' GPUs to share one product name.
- `up` starts the engines with [`narwhal-engine start-native`](Engine.md#own-gpu-startup), which starts each engine when its GPU's free memory covers its fraction and records the engine's memory growth on that GPU in `native-start.json`.
- `up` profiles each engine after it serves warm-up requests, through [`narwhal-profile --warmup-requests 8`](Profile.md#prefill-and-decode-sweeps): each engine serves 8 before its prefill sweep and 8 before its decode sweep. An SGLang engine serves each 8 in the role of the sweep that follows, and a vLLM or TensorRT-LLM engine serves all 16 in its configured role.

An engine of this fleet can [leave the router and rejoin it](../Dev-Runtime.md#removing-and-rejoining-an-engine) while the fleet runs, on vLLM, SGLang and TensorRT-LLM.

To serve another model or runtime, write a [custom template](../Dev-Runtime.md#template-and-gpu-allocation) and select it with `--template`.

## Role-split replay

The RTX 5090 reference template's `role_cycle` holds three seeded workloads that drive the four-engine fleet's controller from 2P:2D to 1P:3D, back to 2P:2D, to 3P:1D and back to 2P:2D. To [replay them](../dev/RTX-5090-Reference.md#replaying-all-three-role-splits), run this from a checkout against an instance that reports `ready`:

```bash
python -m tools.measurement.dev_cycle --instance runs/dev
```

The replay writes its records to a new `cycle-*` directory in the instance, or to `--out`. Its `summary.json` sets `passed` when all of these hold:

- The controller's reactive moves produce exactly the expected splits on the same engine processes.
- Each workload runs on schedule with every request completed or rejected for its TTFT.
- Each workload meets the SLO where the recipe requires it.

The command exits with code `0` when the replay passes and `2` when it fails.

## Run directories and recovery

A fresh `up` writes a `run-*` directory in the instance. It holds:

- `fleet.json`, the router's fleet configuration, with the engine contract for engines from `init`
- the request journal in `journal.jsonl` and the router's state in `router-state.json`
- whole-device VRAM samples in `up-memory.jsonl` and `verify-memory.jsonl`, with one row per GPU and its `gpu_uuid` under `--gpus`
- `launch`, the stage runner's directory, with the [run record](../telemetry/06-Deployment-Runs.md) in `run.json`

Each stage attempt writes its command lines, logs and outputs to its own directory: `launch/stages/<stage>/attempt-<n>`, or `launch/stages/<stage>/engine-<n>/attempt-<n>` for a stage that runs once for each engine. Engine `n`'s launch directory is `engine-<n>` in its `engine-prepare` attempt directory, and on vLLM it also holds the engine's `cache-layout.json`. The profiles are in the `profile` attempt directory.

Each `verify` writes a `verify-*` directory in the run directory with the preflight log, the KV evidence in `kv.json`, the routed response in `completion.json`, and the router and engine metrics.

### Resuming an interrupted `up`

When a stage of `up` fails, or SIGINT or SIGTERM interrupts it, that stage stops the processes it started and `up` exits. The stages that passed keep their engines, sidecars and router running, and `lifecycle.json` records the failure with the phase `failed`. The next `up` resumes in the same run directory and prints `resuming <run directory> at <stage>` to stderr before the first stage it runs. It skips a stage whose last attempt passed, whose recorded inputs still match, whose output files still match their SHA-256 and whose processes still run. An `up` that was killed outright resumes the same way.

To recover:

1. Read the log of the stage that the error names.
2. Repair the cause.
3. Run `narwhal dev up`.

To start afresh instead, run `narwhal dev down` first. It stops every process of the run, and the next `up` writes a new run directory. A change to the instance's settings through `setup`, or through an option of `up`, also needs `down` first.

A change reruns the stages its row lists, and a changed output file reruns its stage and every stage after it. A launch directory starts its engine once, so when an engine of the fleet has stopped, every engine is prepared, checked and started again in fresh launch directories. For engines from `init`:

<!-- Generated from LAUNCHED_GRAPH in src/narwhal/deployment/dev_graph.py by tools/maintenance/stage_graph_doc.py. Edit the graph, then run the tool. -->

| Stage | Runs | After | Gate section |
| --- | --- | --- | --- |
| `package` | Once per run |  | [Building the deployment package](../deploy/02-Install.md#building-the-deployment-package) |
| `engine-prepare` | Once for each engine | `package` | [Preparing, checking, and starting each engine](../deploy/03-Validate-Engines.md#preparing-checking-and-starting-each-engine) |
| `engine-check` | Once for each engine | `engine-prepare` | [Preparing, checking, and starting each engine](../deploy/03-Validate-Engines.md#preparing-checking-and-starting-each-engine) |
| `engine-start` | Once per run | `engine-check` | [Preparing, checking, and starting each engine](../deploy/03-Validate-Engines.md#preparing-checking-and-starting-each-engine) |
| `attest-capture` | Once for each engine | `engine-start` | [Capturing the attestation](../deploy/05-Attest.md#capturing-the-attestation) |
| `sidecar-start` | Once for each engine | `attest-capture` | [Starting the sidecar](../deploy/05-Attest.md#starting-the-sidecar) |
| `finalize-fleet` | Once per run | `sidecar-start` | [Finalizing the fleet contract](../deploy/05-Attest.md#finalizing-the-fleet-contract) |
| `profile` | Once per run | `finalize-fleet` | [Profiling idle engines](../deploy/06-Profile-and-Preflight.md#profiling-idle-engines) |
| `router-start` | Once per run | `profile` | [Starting the router pair](../deploy/07-Serve-and-Measure.md#starting-the-router-pair) |

| Change | Recorded as | Reruns |
| --- | --- | --- |
| Instance settings | The SHA-256 of `instance.json`, `template.json`, `fleet.json` and `engine-launch.json` | `package` and every stage after it |
| Narwhal source | The SHA-256 of the installed package's Python files | `package` and every stage after it |
| Engine API key | The SHA-256 of the key that the fleet's `engine.engine_api_key_env` names, or of `NARWHAL_ENGINE_API_KEY` | `package` and every stage after it |
| Launch directory used by a start whose engines no longer all run | The launch directory and the SHA-256 of its `launch.json`, checked live on every run | `engine-check` for that engine; `engine-start` |
| An engine process restarted | Each engine's PID, boot ID and start ticks, checked live on every run | `attest-capture`, `sidecar-start`, `finalize-fleet` |
| Attested binding digest of an engine | The `launch_digest`, or the `attestation_digest` when the attestation has no launch evidence | `profile`, `router-start` |
| Engine contract fields of an engine | The `EngineContract` fields of its attestation | `attest-capture` for every engine; `finalize-fleet`, `profile`, `router-start` |
| Attestation sidecar restarted | The sidecar's recorded process identity, checked live on every run | `sidecar-start` for that engine; `finalize-fleet` |
| Router restarted | The router's recorded process identity, checked live on every run | `router-start` |

<!-- End of the generated stage tables. -->

For llama.cpp engines from `setup`:

<!-- Generated from DIRECT_GRAPH in src/narwhal/deployment/dev_graph.py by tools/maintenance/stage_graph_doc.py. Edit the graph, then run the tool. -->

| Stage | Runs | After | Gate section |
| --- | --- | --- | --- |
| `package` | Once per run |  | [Building the deployment package](../deploy/02-Install.md#building-the-deployment-package) |
| `engine-start` | Once per run | `package` | [Preparing, checking, and starting each engine](../deploy/03-Validate-Engines.md#preparing-checking-and-starting-each-engine) |
| `profile` | Once per run | `engine-start` | [Profiling idle engines](../deploy/06-Profile-and-Preflight.md#profiling-idle-engines) |
| `router-start` | Once per run | `profile` | [Starting the router pair](../deploy/07-Serve-and-Measure.md#starting-the-router-pair) |

| Change | Recorded as | Reruns |
| --- | --- | --- |
| Instance settings | The SHA-256 of `instance.json`, `template.json` and `fleet.json` | `package` and every stage after it |
| Narwhal source | The SHA-256 of the installed package's Python files | `package` and every stage after it |
| An engine process restarted | Each engine's PID, boot ID and start ticks, checked live on every run | `profile`, `router-start` |
| Router restarted | The router's recorded process identity, checked live on every run | `router-start` |

<!-- End of the generated stage tables. -->

The gate sections describe the stage of the same name in [a fleet deployment](../Deploy.md#rerunning-after-a-change).

To find a stage's time budget or handle processes that survive `down`, follow [Stage deadlines and recovery](../Dev-Runtime.md#stage-deadlines-and-recovery).
