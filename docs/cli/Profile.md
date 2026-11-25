---
description: Measure the prefill and decode cost model of each engine with narwhal-profile sweeps.
---

# `narwhal-profile`

`narwhal-profile --fleet PATH` measures the prefill and decode costs of vLLM, SGLang and TensorRT-LLM engines and writes them as router profiles. It runs in one of three modes:

| Mode | Operation | Output |
| --- | --- | --- |
| Live sweep | Measures the live engines. | The fleet's `profiles.path`, or `--out` |
| Refit | Recomputes the cold and warm time to first token (TTFT) fits from a sample sidecar and keeps the decode fits. | `--out` |
| Merge | Combines profile stores. | `--out` |

Every mode writes a profile store, and a sample sidecar at the store's path with the suffix `.samples.json`. Neither file may be a symlink.

A live sweep binds each fit to the [process generation](../configuration/01-Fleet-Schema.md#attestation) of the engine it measured.

A live sweep measures engines on separate devices at the same time. Engines in one `shared_device.group`, and all engines under `--colocated`, take turns.

## SGLang pairs

On an SGLang fleet, the sweep measures each engine in both roles while a peer engine serves the other leg of each request. The fleet needs `engine.connector` `mooncake`, at least two engines and an attestation sidecar for each engine. Before each of an engine's sweeps, the sweep switches the engine and its peer into opposite roles with `POST /pd_role_switch` and empties both caches with `POST /flush_cache`. When the sweep ends, even after a failure, the profiler returns each switched engine to its earlier role. It prints `role restore failed: <error>` for an engine it cannot switch back, restores the rest, and exits with code `1`. It prints `role unreadable, not restored: <url>` for an engine whose role it could not read beforehand.

The selected engines pair up in fleet order, and the pairs run at the same time. With an odd count, the last three engines form a ring in which each engine's peer is the next. A single selected engine pairs with the next engine in the fleet. When any selected engine sets `shared_device`, the engines run one at a time, each paired with the next selected engine, or under `--colocated` with an engine from its [group](#shared-gpu-neighbour-traffic).

### Router drain check

A role switch fails the requests an engine holds, and a router keeps sending requests to an engine it has not drained. An SGLang sweep with `--only` therefore requires `--router-url` with the active router's URL, or `--no-router` when no router serves the fleet's engines. Without either, the profiler exits with code `2` before it contacts any engine. A sweep of every engine requires neither. vLLM and TensorRT-LLM sweeps switch no engine roles and ignore both options.

With `--router-url`, before it contacts any engine, the profiler reads [`GET /narwhal/engines`](../http-api/08-Engine-Membership.md#get-narwhalengines) and [`GET /narwhal/lifecycle`](../http-api/07-Handoff-and-Lifecycle.md#get-narwhallifecycle) from the router. It checks each engine the sweep uses: the selected engines, their peers and, under `--colocated`, their group neighbours. A router member matches an engine when it has the same `iid`, or a URL with the same port and either the same host name or a host that resolves to a shared address, so `localhost` matches `127.0.0.1`. A matching member must have one of these lifecycle records:

- `state` `drained` and `ready_to_stop` `true`
- `state` `blocked`, `accepts_new` `false`, no `resident` requests and `pending_handoffs` `0`

An ejected engine reads `active` and fails the check, because the router's automatic recovery can return it to placement during the sweep. The profiler exits with code `1` when the router is not the active router, when it cannot read either document, or when a member fails the check. The error names each failing engine, as `peer <iid>` when it is not selected, with its lifecycle `state`:

```text
refusing to switch engines that the router at http://router:8000 has not drained: peer e1 is active. Drain each one with POST /narwhal/lifecycle/drain and wait until GET /narwhal/lifecycle reports it ready_to_stop, as in Joining and removing engines. To pair a selected engine with an engine outside the router's fleet instead, sweep with a fleet file that lists that engine right after it
```

The router holds one engine at a time outside a whole-wave drain. When the router holds the selected engine and its peer is a member, pair the selected engine with a spare engine outside the router's fleet, or hold both through a whole-wave drain, as [Refresh profiles](../operate/03-Restart-Engines.md#refresh-profiles) describes. Under `--colocated`, the peers come from the selected engine's `shared_device` group, so only the whole-wave drain applies, and the error leaves out the spare-engine sentence.

An engine outside the router's membership passes, and the profiler prints `router <url> has no member with the ID or URL of: <iid>` for it.

## Selection, refitting, and output

These options select engines, choose the mode, and set the output files:

| Option | Default | Description |
| --- | --- | --- |
| `--version` | | Print the installed distribution version. |
| `--format` | `text` | Output format, either `text` or `json` for [versioned command results](../Command-Results.md). |
| `--fleet PATH` | required | Fleet configuration file that defines engine membership for every mode. |
| `--only IID` | every engine | Engine `iid` to include in a live sweep, repeatable, or the one engine that `--copy-from` writes a profile for. |
| `--refit-samples PATH` | optional | Sample sidecar to refit. |
| `--merge PATH` | optional | Profile store to combine, read with its matching sample sidecar, repeatable. |
| `--copy-from IID` | optional | Member whose measured profile at `profiles.path` to copy to the `--only` engine, as [Copying a member's profile](#copying-a-members-profile) describes. |
| `--out PATH` | optional | New profile store, written with a matching sample sidecar. Required for `--refit-samples`, `--merge` and `--copy-from`. A live sweep writes here in place of `profiles.path`. |
| `--limits PATH` | requested concurrency points | [`profiling-limits.json`](../deploy/06-Profile-and-Preflight.md) with each engine's `runtime.max_sequences`, above which the profiler drops decode concurrency points. |
| `--observation-timeout-s SECONDS` | each probe's built-in timeout | Finite, positive HTTP timeout for every live probe. |
| `--overwrite` | `false` | Replace the live profile store and sample sidecar, starting when the first engine completes. |
| `--metrics-url URL` | `$NARWHAL_TOOL_METRICS_URL` | Serve [tool metrics](../telemetry/03-Metrics-and-Control.md#tool-metrics) at `URL/metrics` during a live sweep, where `URL` is `http://host:port`. |
| `--router-url URL` | optional | Active router that must report every engine an SGLang sweep uses as drained or outside its fleet, as [Router drain check](#router-drain-check) describes. `URL` starts with `http://` or `https://`. vLLM and TensorRT-LLM sweeps ignore it. |
| `--no-router` | `false` | State that no router serves the fleet's engines, for an SGLang sweep with `--only`. |

The mode options depend on each other:

| Option | Requires | Mutually exclusive with |
| --- | --- | --- |
| `--refit-samples` | `--out`, and samples covering every fleet engine | `--only`, `--merge` |
| `--merge` | `--out` | `--only`, `--overwrite`, `--refit-samples` |
| `--copy-from` | `--out`, and one `--only` | `--merge`, `--overwrite`, `--refit-samples` |
| `--out` | A store path and sidecar path that do not exist yet | `--overwrite` |
| `--observation-timeout-s` | A live sweep | `--refit-samples`, `--merge`, `--copy-from` |
| `--metrics-url` | A live sweep | `--refit-samples`, `--merge`, `--copy-from` |
| `--router-url` | A live sweep | `--no-router`, `--refit-samples`, `--merge`, `--copy-from` |
| `--no-router` | A live sweep | `--router-url`, `--refit-samples`, `--merge`, `--copy-from` |

With `--only`, `--overwrite` writes a store that holds only the selected engines' profiles.

A refit needs each engine's saved generation evidence, and writes the raw samples and that evidence to its new sample sidecar.

A merge needs measurement evidence in each source's sample sidecar that matches every profile in the source store. The merged stores must cover every configured engine. A later store replaces an earlier store's row for the same combination of engine, GPU group, role split and target role. The merge leaves out rows for engines outside the fleet. A merged store can be a source. The merged sample sidecar records each source file's SHA-256 hash and its path relative to the merged store's directory. Keep the source files at those paths.

Run each mode with:

```bash
narwhal-profile --fleet fleet.json
narwhal-profile --fleet fleet.json --refit-samples profiles.samples.json --out refitted.json
narwhal-profile --fleet fleet.json --merge split-1.json --merge split-2.json --out combined.json
narwhal-profile --fleet fleet.json --copy-from e0 --only e4 --out copied.json
```

### Copying a member's profile

`--copy-from` gives a new engine the measured profile of a member without a sweep, so the new engine needs no drained peer and no idle time. It reads the member's row at `profiles.path` and the matching evidence in its sample sidecar, then checks the `--only` engine, which must be in the `--fleet` file:

- The fleet sets a complete `engine_contract`.
- The engine's attestation matches the contract, and its launch evidence matches the generation evidence of the member's profile: the launch arguments other than per-engine flags such as ports, the image, the expected packages, the launcher code, and the model files in `model_config_sha256`, `model_tree_sha256` and `model_sha256`. The check leaves out `launch_sha256`, the digest of each engine's whole launch file, because ports and devices make it differ between engines. It therefore does not compare the engines' environment variables, so give the new engine the member's runtime environment.
- The engine reports the same KV capacity, in tokens, as the member's `kv_capacity_tokens`.
- The member's profile is measured, not nominal, and outside a shared-device group.

Any failed check stops the copy with exit code 2 and no output. The attestation does not record the GPU model, so copy a profile only between engines on the same GPU type. The output store holds only the new engine's row, bound to the new engine's own generation digest, and its sample sidecar records the member's evidence, the new engine's generation evidence and `copied_from`, the member and the store it came from. Merge it into the fleet's store as you would a swept profile.

## Prefill and decode sweeps

Every mode validates these options:

| Option | Default | Description | Valid values |
| --- | :---: | --- | --- |
| `--prefill-lens LIST` | `256,700,1024,1300,2300,4096,4300,8300,12300,16300` | Comma-separated candidate prefill lengths. Each engine keeps the lengths whose input plus one output token stays below its [context limit](../measure/01-Profile.md#prefill-sweep). | At least three distinct positive values, and three that remain for each engine |
| `--decode-input-lens LIST` | `512,4096,8192` | Comma-separated prompt lengths for the decode sweep. Each engine keeps the lengths whose input plus `--decode-tokens` stays below its [context limit](../measure/01-Profile.md#decode-sweep). | At least two distinct positive values, and two that remain for each engine |
| `--decode-concurrency LIST` | `1,4,16,48` | Candidate stream counts. [Decode sweep](../measure/01-Profile.md#decode-sweep) describes how each engine's sequence limit and KV capacity change them. | At least two distinct values of at least 1, and two that remain for each engine |
| `--decode-tokens N` | `64` | Tokens per decode stream. | At least 3 |
| `--cached-prefix-lens LIST` | `2048,4096,8192` | Comma-separated cached prefix lengths for the warm prefill sweep. | Positive, with at least two distinct values. The number of distinct prefix lengths times the number of distinct `--cached-suffix-lens` lengths must be at least 5. |
| `--cached-suffix-lens LIST` | `700,1300,2600` | Comma-separated uncached suffix lengths for the warm prefill sweep. | Positive, with at least two distinct values |
| `--prefill-repeats N` | `3` | Repetitions per prefill length. | At least 3 |
| `--decode-repeats N` | `1` | Repetitions per decode input length and concurrency. | At least 1 |
| `--warmup-requests N` | `0` | Requests each engine serves before its sweep. An SGLang engine serves `N` in the prefill role and then `N` in the decode role. A vLLM or TensorRT-LLM engine serves `2N` in its configured role. Each request takes at most the shortest prefill length as input, shortened to leave room for `--decode-tokens` within the context limit, and asks for `--decode-tokens` output tokens. When `N` is above 0, the sample sidecar's `sweep` record holds it as `warmup_requests`. | At least 0 |

The live context limit comes from `/tokenize` on vLLM. On SGLang, it is the lower of `context_length` and `max_req_input_len` on `/server_info`. On TensorRT-LLM, it is `--max_seq_len` in the engine's attested launch.

The [warm prefill sweep](../measure/01-Profile.md#warm-prefill-with-a-cached-prefix) runs when the engine exports a prefix-cache hit counter, `vllm:prefix_cache_hits_total` on vLLM, `sglang:cached_tokens_total` on SGLang or `trtllm_prompt_cached_tokens_total` on TensorRT-LLM. When a [warm sweep condition](../measure/01-Profile.md#warm-prefill-with-a-cached-prefix) fails, the profile keeps cold prefill only, and the sample sidecar records the reason in `cached_prefill.reason`.

When the fleet file sets `serving.prefill_on_decode`, each engine's sweep ends with the [prefill-share step](../telemetry/02-Profiles.md#prefill-share-on-decode-engines). When the step fails, the engine's profile is written without the prefill-share fit.

## Shared-GPU neighbour traffic

With `--colocated`, each target needs at least one neighbour in its `shared_device.group`, and the sweep measures the target while its neighbours serve paced traffic in their configured roles. On an SGLang fleet, each target pairs with the first engine in its group that has the other configured role, and the group keeps its configured prefill and decode counts while the target measures both roles.

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--colocated` | `false` | Measure each target under traffic from the neighbours in its `shared_device.group`. | |
| `--neighbour-prefill-rps RATE` | required with `--colocated` | Offered requests per second per prefill neighbour. | Finite, positive |
| `--neighbour-decode-rps RATE` | required with `--colocated` | Offered requests per second per decode neighbour. | Finite, positive |
| `--neighbour-prefill-tokens N` | required with `--colocated` | Input tokens per prefill neighbour request, each asking for one output token. | Positive integer |
| `--neighbour-decode-input-tokens N` | required with `--colocated` | Input tokens per decode neighbour request. | Positive integer |
| `--neighbour-decode-output-tokens N` | required with `--colocated` | Output tokens per decode neighbour request. | Positive integer |

```bash
narwhal-profile --fleet fleet.json --colocated \
  --neighbour-prefill-rps 0.5 --neighbour-decode-rps 0.25 \
  --neighbour-prefill-tokens 512 --neighbour-decode-input-tokens 128 \
  --neighbour-decode-output-tokens 32
```

The sample sidecar's `colocated_load` records each neighbour's role, completions, achieved rate and error. On SGLang, the rates leave out `paused_s`, the seconds neighbour traffic paused for role switches, and `completed_by_role` holds the peer's completions in each role.

## Completion and exit codes

On success, each mode ends with this line and exits with code `0`:

| Mode | Success output |
| --- | --- |
| Live sweep | `wrote N profile(s) to PATH` |
| Refit | `refitted N profile(s) to PATH` |
| Merge | `merged N measured profiles to PATH` |

A live sweep writes the profile store and sample sidecar after each engine completes, so a failure keeps the profiles of engines that finished. The sidecar also records the failing engine's error. In text mode, the profiler stops with exit code `1` when:

- an engine's `/health` returns a status other than 200
- a probe, including the context-limit probe, fails or returns an incomplete response
- a warm-up request returns a status other than 200
- an engine serves prompt tokens from its prefix cache during the prefill sweep, the decode sweep, or a cold control of the warm prefill sweep
- the decode fit error exceeds `profiles.max_decode_fit_mape`, its leave-one-cell-out error exceeds `profiles.max_decode_cv_mape`, or its measured domain is a single point on either axis
- a neighbour fails a request or completes zero requests during the target's measurement
- on SGLang, neighbour requests still run `--observation-timeout-s` seconds, 300 by default, after neighbour traffic pauses
- a neighbour's input plus output exceeds the neighbour's context limit
- the prefill fit exceeds 20% mean error or 50% worst-point error
- an engine's attestation fails verification, or its process changes during its measurement
- on SGLang, `POST /pd_role_switch` fails or the engine refuses the switch, or `POST /flush_cache` fails or returns a status other than 200
- on SGLang with `--router-url`, the router fails the [router drain check](#router-drain-check)

It stops with exit code `2` when:

- the arguments or the fleet configuration fail validation
- `--only` matches zero configured engines
- an output exists without `--overwrite`, or a refit or merge output exists
- a fleet with `engine_contract` has a profiled engine without an attestation URL
- an SGLang fleet uses a connector other than `mooncake`, has one engine, or lacks an attestation URL
- an SGLang sweep with `--only` has neither `--router-url` nor `--no-router`
- a `--colocated` target has no neighbour in its group
- on SGLang, a `--colocated` target has no engine of the other configured role in its group
- an engine's context limit leaves fewer than three prefill lengths, two decode input lengths, or two decode concurrency levels
- a refit or merge input fails its checks

With `--format json`, the exit code follows the [command result](../Command-Results.md) status.
