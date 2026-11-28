---
description: Run the narwhal-check preflight gates against profiled, idle Narwhal engines.
---

# Profile engines and run preflight

The sixth gate measures the idle engines, calibrates the first-token deadline, writes the service-level objective (SLO) targets and the deadline into the routers' fleet, and runs the preflight checks on each router host:

| Stage | Runs | Evidence in its attempt |
| --- | --- | --- |
| `profile` | Once per run on the primary router's host, after `finalize-fleet` | `fleet.json`, the fleet it profiled, and `host/`: `profiles.json` and `profiles.samples.json` |
| `calibrate` | Once per run on the primary router's host, after `profile` | `host/`: `first-token-calibration.json` and the fleet it read |
| `fleet-limits` | Once per run on the workstation, after `calibrate` | `fleet.json`, the routers' fleet, with copies of the profiles and the calibration |
| `preflight` | Once for each router host, after `fleet-limits` | `host/preflight-evidence.json` and the `narwhal-check` output in `logs/` |

Their inputs are the profiling, calibration, SLO and margin variables of [Deployment run inputs](../configuration/04-Deployment-Inputs.md#deployment-run-inputs). The engines must be otherwise idle: no router serves them until `router-start`.

## Profiling idle engines

`profile` runs on the primary router's host, in its role environment, with the fleet that `finalize-fleet` wrote and the profile store in its attempt directory:

```bash
narwhal-profile --fleet <attempt>/fleet.json --limits runs/deployment/profiling-limits.json \
  --prefill-lens "$NARWHAL_PROFILE_PREFILL_LENS" \
  --decode-input-lens "$NARWHAL_PROFILE_DECODE_INPUT_LENS" \
  --warmup-requests "$NARWHAL_PROFILE_WARMUP_REQUESTS"
```

It adds `--decode-concurrency "$NARWHAL_PROFILE_DECODE_CONCURRENCY"` when that is set, and `--no-router` on SGLang, whose sweep switches each engine's peer while no router serves them. Choose the sweep from your production traffic:

- `NARWHAL_PROFILE_PREFILL_LENS` values that meet the [`ttft_split` length rules](../measure/01-Profile.md#prefill-sweep).
- `NARWHAL_PROFILE_DECODE_INPUT_LENS` values near the input lengths you serve.
- `NARWHAL_PROFILE_DECODE_CONCURRENCY` values near the concurrency you serve, when it differs from the default `1,4,16,48`.

For each engine the profiler prints `<iid>: max_model_len <n>; prefill up to <x>, decode input up to <y>, decode concurrency up to <z>`, which shows how far the sweep reached within that engine's checked serving plan.

`narwhal deploy package` writes each engine's `runtime.max_sequences` to `profiling-limits.json`. An SGLang engine without the setting gets 256 and a TensorRT-LLM engine without it gets 64.

The profiler fits each sweep to the engine's live `max_model_len`, as [Prefill sweep](../measure/01-Profile.md#prefill-sweep) and [Decode sweep](../measure/01-Profile.md#decode-sweep) define. It ends decode concurrency at the engine's sequence limit.

Prefix caching can stay on during profiling. On an SGLang fleet, `narwhal-profile` requires the `mooncake` connector and at least two engines.

When an engine refuses a decode measurement request, reports an error in its stream, or times out or drops the stream (for example after a lost KV transfer), the profiler discards that cohort's measurement and runs it again, up to twice. The log shows each rerun as `rerun after engine error`. A third consecutive error stops the profile.

If the live context leaves fewer than three prefill lengths or two decode input lengths, the stage fails: set shorter `NARWHAL_PROFILE_PREFILL_LENS` and `NARWHAL_PROFILE_DECODE_INPUT_LENS` and run again. If the sequence limit leaves fewer than two decode concurrency points, change the engine launch policy.

The stage also fails when the prefill curve does not fit the per-length medians within 20% mean error or 50% worst-point error, as [Prefill fit](../measure/01-Profile.md#prefill-fit) defines:

```text
narwhal-profile: ... n1: prefill median fit error 12.9%, worst point 55.6%; inspect the retained per-length measurements before using this profile
```

The medians are in `host/profiles.samples.json` in the stage's attempt directory. A small model on a fast GPU can give a flat curve with a step at short prompts, such as 17.8 ms at 256 tokens, 33.8 ms at 700 and 64.1 ms at 12,300, and the fit misses the shortest length. Drop the lengths below the step from `NARWHAL_PROFILE_PREFILL_LENS` and run again. Keep at least three lengths, and keep two on cache-block boundaries, such as 1024, 4096 and 8192, so the profiler can still set `ttft_split`, as [Prefill sweep](../measure/01-Profile.md#prefill-sweep) describes:

```bash
NARWHAL_PROFILE_PREFILL_LENS=1024,1300,2300,4096,4300,8192,8300,12300
```

The profiler's `.samples.json` sidecar stays beside the `profiles.path` store, and `fleet-limits` copies both.

Preflight warns when an engine's profile measures decode below its sequence limit. It reads that limit from the profile's sidecar (recorded by `--limits`) and, on a fleet with an engine contract, from the engine's attestation when the sidecar records a lower limit or none. Decode seats stop at the profiled maximum, as [Engine seats](../configuration/02-Serving-and-Role-Control.md#engine-seats) describes. To clear the warning, re-profile the engine with `--limits`, after you:

- regenerate the limits file, when the sidecar records a lower limit than the attestation.
- set shorter decode input lengths, or lower the engine's sequence limit and regenerate the limits file, when the sweep skipped cohorts that exceed the engine's KV capacity.

Preflight fails and router startup stops when the profile engine IDs differ from the configured fleet. A standby router also starts when its store has profiles for engines outside the fleet. When a live engine's process generation differs from its saved profile, preflight and router startup require a [new profile](#reusing-or-repeating-profiles). The role controller holds a role change whose projected decode point falls outside the measured profile range.

Set `NARWHAL_TTFT_S` and `NARWHAL_TPOT_S`, which `fleet-limits` writes as `slo.ttft_s` and `slo.tpot_s`, from the service requirement and the measured engine curves, as [Setting production SLOs](../measure/02-Targets-and-Freeze.md#setting-production-slos) describes. Keep the time per output token (TPOT) target above the measured per-token floor.

### Reusing or repeating profiles

A run profiles every engine again when the profiling inputs or an engine's attested binding digest change, as the [rerun table](../Deploy.md#rerunning-after-a-change) lists. On a fleet in service, re-profile only the engines the `profile` gate of preflight names, with the same sweep options, and merge the new store into the current `profiles.path` store:

```bash
.venv/bin/narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --limits runs/deployment/profiling-limits.json \
  --prefill-lens 256,700,1300,2300,4096,4300,8300,12300 \
  --decode-input-lens 512,4096,8192 \
  --only <iid> --out runs/profiles-<iid>.json
.venv/bin/narwhal-profile --fleet runs/deployment/fleet.json \
  --merge runs/profiles.json --merge runs/profiles-<iid>.json \
  --out runs/profiles-merged.json
```

On an SGLang fleet, the sweep also switches each selected engine's peer, so add `--router-url` with the active router's URL, or `--no-router` when no router serves the fleet's engines. With `--router-url`, the router must hold every selected engine and peer that it serves, as [Router drain check](../cli/Profile.md#router-drain-check) describes. The router holds one engine at a time outside a whole-wave drain, so when the selected engine and its peer are both members, profile during a whole-wave drain or pair the engine with a spare engine outside the fleet, as [Refresh profiles](../operate/03-Restart-Engines.md#refresh-profiles) describes. A sweep with `--colocated` takes its peers from the engine's own `shared_device` group, so it needs the whole-wave drain.

The later store replaces the earlier store's row for each engine variant it holds. After you remove an engine from the fleet, merge the current store alone to write a store without that engine's rows.

Set `profiles.path` to the merged store and run preflight. To load it into a running router, follow [Activating replacement profiles](../operate/03-Restart-Engines.md#activating-replacement-profiles).

## Calibrating the first-token deadline

`calibrate` runs on the primary router's host after `profile`, with the fleet and profiles `profile` wrote and the engines idle:

```bash
narwhal-check --fleet <attempt>/fleet.json --calibrate-first-token \
  --input-tokens "$NARWHAL_CALIBRATION_INPUT_TOKENS" \
  --samples "$NARWHAL_CALIBRATION_SAMPLES" \
  --observation-timeout-s "$NARWHAL_CALIBRATION_OBSERVATION_TIMEOUT_S" \
  --calibration-out <attempt>/first-token-calibration.json
```

Choose the inputs:

| Input | Value |
| --- | --- |
| `NARWHAL_CALIBRATION_INPUT_TOKENS` | Lengths spanning the served range, up to the longest input that leaves room for two output tokens on both engines. For engines whose live context limit is 16,384 tokens, `256,8192,16382` |
| `NARWHAL_CALIBRATION_OBSERVATION_TIMEOUT_S` | Above the fleet's `engine.first_token_timeout_s` and at most `serving.request_timeout_s`, which the command requires |
| `NARWHAL_CALIBRATION_SAMPLES` | At least 100, the minimum for qualifying evidence |

On SGLang the live context limit is the smaller of `context_length` and `max_req_input_len`, which is below the launch's context length.

The command exits 0 when the artifact is [`complete`](../cli/Check.md#calibration-artifact) and every engine returns to its starting role. Otherwise it exits 1 and the stage fails.

`serving.request_timeout_s` bounds each attempt's prompt sizing, prefill, and decode completion. Each attempt records one of these statuses:

| Attempt status                                       | Meaning                                                          |
| ---------------------------------------------------- | ---------------------------------------------------------------- |
| `completed`                                          | Decode produced its first token within `--observation-timeout-s` |
| `request_expired`                                    | `serving.request_timeout_s` expired                              |
| `observation_expired`                                | The first token missed `--observation-timeout-s`                 |
| `failed_sizing`, `failed_prefill`, `failed_transfer` | The named phase failed                                           |

Only `completed` timings enter the candidate calculation.

For four engines in separate device slots, the command prints:

```text
calibration groups: 36; concurrent rounds per input length: 3; sweep 1 runs each group alone
n1 -> n2, target 256 tokens: 100/100 completed, 0 failed
...
first-token calibration: runs/deployment/first-token-calibration.json
candidate deadline: above 1.840s
duration 1200s
```

Diagnose failed transfers and observation expiries from the attempt's log before you rely on the candidate.

The client time to first token (TTFT) target must hold the deadline, the slowest engine's prefill of the longest calibrated input and the router overhead. Preflight's [`slo` gate](../cli/Check.md#the-slo-gate) fails with each term when they exceed `slo.ttft_s`:

```text
first-token deadline 2.100s + n3 prefill of 4096 tokens 0.412s + router overhead 0.050s = 2.562s exceeds slo.ttft_s 2.500s
```

### Reusing or repeating the calibration

`fleet-limits` runs on the workstation after `calibrate`. It writes into the primary router's fleet:

- `slo.ttft_s`, `slo.tpot_s` and `slo.router_overhead_s` from `NARWHAL_TTFT_S`, `NARWHAL_TPOT_S` and `NARWHAL_ROUTER_OVERHEAD_S`
- `engine.first_token_timeout_s`, the calibrated candidate plus `NARWHAL_FIRST_TOKEN_MARGIN_S`
- `engine.first_token_calibration_path`, `profiles.path` and `recovery.state_path`, in its attempt directory

It then copies that fleet, the calibration and the profiles to the same path on every router host, and prints the deadline it chose. A calibration without a candidate deadline fails the stage. The stage has no command of its own: `narwhal deploy run --until fleet-limits` runs it once the stages before it hold. A changed SLO or margin repeats `fleet-limits` and what follows, without profiling or calibrating again.

A run calibrates again when the calibration inputs, the profiles, a fabric route or an engine's binding digest change. On a fleet in service, run preflight after a change to the fleet or its engines. When the `calibration` gate fails, apply the fix its message names, or recalibrate when it names none. Also recalibrate when either of these changes:

- the fabric route or host behind an unchanged engine URL
- an engine's context limit, when it exceeds the longest calibrated input by more than two tokens

The artifact stays valid when you remove or pin an engine.

To measure only the groups a previous artifact lacks or did not complete, pass that artifact with `--calibration-base`, with the same `--input-tokens` and `--samples`. When an artifact records a length that leaves room for fewer than two output tokens, recalibrate without `--calibration-base`.

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json \
  --calibrate-first-token --input-tokens 256,8192,16382 \
  --samples 100 --observation-timeout-s 20 \
  --calibration-base runs/deployment/first-token-calibration.json \
  --calibration-out runs/deployment/first-token-calibration-2.json
```

Point `engine.first_token_calibration_path` at the new artifact on every router host before preflight.

## Running preflight

`preflight` runs on each router host after `fleet-limits`, in its role environment, with the fleet `fleet-limits` wrote:

```bash
narwhal-check --fleet <fleet-limits attempt>/fleet.json --evidence-out <attempt>/preflight-evidence.json
```

A failed gate fails the stage. Follow the reported engine, transfer leg or budget into [Troubleshooting a fleet](../Troubleshoot.md), fix the cause and run again.

The full preflight runs these gates:

| Gate          | What must pass                                                                                                                                               |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `reach`       | Every engine returns HTTP 200 within the configured health HTTP I/O timeout                                                                                  |
| `calibration` | The configured first-token calibration is valid, matches every running process generation, and has a candidate deadline below `engine.first_token_timeout_s` |
| `contract`    | Attestation matches the current process and declared runtime, and every engine attests the same model files                                                  |
| `profile`     | Each saved process generation digest matches its live engine, the profile IDs match the fleet, and the measured decode errors stay within policy             |
| `model`       | Every engine serves the configured model                                                                                                                     |
| `pace`        | Prefill latency stays within the [permitted slowdown](#pace-gate)                                                                                            |
| `tokenize`    | Exact input sizing succeeds when `engine.tokenize` is on                                                                                                     |
| `hca`         | Every sidecar of an engine that sends KV through its HCA, even on its own host, publishes that HCA, and its transmit counter when the engine has a peer on its host |
| `produce`     | Every tested vLLM producer can export a KV handoff; an SGLang producer passes here, and its consume pairs check the transfer                                 |
| `consume`     | Every tested peer can consume that KV handoff                                                                                                                |
| `slo`         | The configured TTFT and TPOT targets are feasible against the measured profiles                                                                              |

Preflight and router startup report the first-token calibration:

| Calibration                                                                   | Preflight          | Router startup         |
| ----------------------------------------------------------------------------- | ------------------ | ---------------------- |
| `engine.first_token_calibration_path` is empty                                | `WARN`             | Logs a warning         |
| The artifact fails validation                                                 | `FAIL`             | Stops                  |
| An engine's process generation differs from the artifact                      | `FAIL`             | Stops                  |
| `engine.first_token_timeout_s` is at or below the candidate deadline          | `FAIL`             | Stops                  |
| Every engine runs the measured process                                        | `ok` measured line | Logs the measured line |
| One or more engines run a relaunched process with the same process generation | `ok` reused line   | Logs the reused line   |

For a fleet that runs the measured processes, preflight prints:

```text
calibration
  ok    first-token calibration measured on the running engines: candidate 1.840s, deadline 2.5s
```

For a fleet with relaunched engines `n2` and `n4`, preflight prints:

```text
calibration
  ok    first-token calibration reused for n2, n4: launch unchanged since capture; candidate 1.840s, deadline 2.5s
```

### Pace gate

Preflight fails the [`pace` gate](../cli/Check.md#preflight) when a probe fails.

When three or more engines probe successfully, the gate compares each engine against the fleet median. It compares every profiled engine against its profile's prefill prediction at the exact `usage.prompt_tokens`. With fewer than three successful engines, an unprofiled engine skips the gate.

### KV transfer gates

[`narwhal-check`](../cli/Check.md) selects transfer pairs with these options:

| Option        | Pairs and probes                                         |
| ------------- | -------------------------------------------------------- |
| Default mesh  | Every eligible ordered pair                              |
| `--ring`      | The pairs covering each eligible producer and consumer   |
| `--repeats N` | `N` transfer probes per pair, with one verdict per probe |

In the consume gate, a transfer across a role-permitted engine pair passes when:

- Decode produces its first generated token within `engine.first_token_timeout_s`. On SGLang, the deadline is at most 30 seconds.
- Decode finishes a valid stream with output.
- Prefill and decode complete within `serving.request_timeout_s`.

The gate reports the first-token time for each passing transfer.

[Fabric qualification](04-Qualify-Fabric.md) measures KV between hosts. Preflight checks KV between engines of one host when those engines send it through their HCAs: their launches set `transfer.transport` to `ucx_rdma`, `transfer.gpu_tls` to `cuda_copy` and `UCX_SHM_DEVICES` to `none`, with one HCA port in `transfer.net_devices`. For each probed pair of such engines on one host, the producer's HCA transmit counter must grow during the transfer, or the pair fails because UCX moved the KV through shared memory or CUDA IPC. An engine with no peer on its host needs no counter. [Same-host HCA path](../cli/Check.md#same-host-hca-path) gives the lines it prints. Under `--ring`, an engine whose same-host pair the ring never probes is skipped, so run the full mesh to qualify the path.

If the first-token deadline expires, check the calibration artifact and the deadline. For any other failure, keep the pair, the error, and the engine logs.

[Serve and measure](07-Serve-and-Measure.md)
