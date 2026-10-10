---
description: Measure relayed frames per router CPU-second for Narwhal router processes against simulated engines.
---

# Benchmarking the router

The router benchmark measures streamed frames relayed per CPU-second of one router process, with simulated engines serving the fleet. The `scale` command runs several router processes against one set of simulated engines.

A relayed frame is a token-bearing `data:` event a client receives, counted as [`token_events`](04-Reconcile-and-Accept.md#10-joining-client-offers-to-the-router-journal) in each client row.

Router CPU seconds are the router process's user and system time from `/proc/<pid>/stat`.

## Requirements

| Requirement | Value |
| --- | --- |
| Host | Linux with `/proc` and `/sys/devices/system/cpu` |
| Python environment | `make setup`, which installs `uvloop`, `httptools` and `httpx` |
| CPUs | `2 + --clients + --engines` for `run` and `compare`, or `1 + --routers + --clients + --engines` for `scale`, plus the router CPUs' SMT siblings listed in `--cpus` |
| Router source | A directory holding the `narwhal` package, such as `src` |
| Open files | The driver raises its soft open-file limit to the hard limit |

## Simulated engines

`tools/measurement/simulated_engine.py` serves one simulated engine per process. `--backend` selects the engine backend whose protocol it serves; the default, `vllm`, is the only one.

Each engine answers these routes:

| Route | Response |
| --- | --- |
| `GET /health` | HTTP 200 with an empty body |
| `GET /version` | `{"version":"simulated"}` |
| `GET /metrics` | The metrics in [Engine metrics](#engine-metrics) |
| `POST /tokenize` | `count`, `max_model_len`, `tokens` and `token_strs` |
| `POST /v1/completions` with `stream: false` | One completion after `--prefill-seconds`, with `kv_transfer_params` for a remote-decode request |
| `POST /v1/completions` with `stream: true` | Paced server-sent events for `max_tokens` tokens |
| Other paths | HTTP 404 |

The simulated tokenizer maps each prompt character to one token ID, the character's Unicode code point.

Each decode stream sends these frames in order:

| Frame | Contents |
| --- | --- |
| First token frame | `prompt_token_ids` with the prompt's token IDs, and one ID in `token_ids` |
| Token frame | One ID in `token_ids` and `finish_reason: null` |
| Last token frame | `finish_reason: "length"` |
| Usage frame | `usage` with `prompt_tokens`, `total_tokens` and `completion_tokens`, for requests with `stream_options.include_usage` |
| Terminator | `data: [DONE]` |

`prompt_token_ids` and `token_ids` carry IDs for requests with `return_token_ids: true` and hold `null` for other requests.

Token `i` has ID `1000 + i % 1000`.

These settings pace each stream:

| Setting | Effect |
| --- | --- |
| `--token-interval` | Seconds between tokens of one stream |
| `--frames-per-write` | Token frames per socket write, one HTTP chunk per frame |
| Tick period | `--frames-per-write` × `--token-interval` |
| Late tick | A tick that starts one tick period or more after its scheduled time, counted in `simulated_engine_late_ticks_total` |

### Engine metrics

| Metric | Type | Value |
| --- | --- | --- |
| `process_start_time_seconds` | Gauge | Engine start time in Unix seconds |
| `simulated_engine_late_ticks_total` | Counter | Late ticks |
| `simulated_engine_active_streams{phase="prefill"}` | Gauge | Prefill requests in progress |
| `simulated_engine_active_streams{phase="decode"}` | Gauge | Decode streams in progress |
| `simulated_engine_peak_streams{phase="prefill"}`, `simulated_engine_peak_streams{phase="decode"}` | Gauge | Most requests of the phase in progress at once since the engine started |

### Running one engine

Start one engine from the repository root:

```bash
.venv/bin/python tools/measurement/simulated_engine.py --iid e0 --port 9001
```

The engine prints one ready line and serves until SIGTERM.

Example ready line:

```text
{"iid": "e0", "url": "http://127.0.0.1:9001", "pid": 4242, "version": "simulated", "process_start_time_seconds": 1791100000.0}
```

| Flag | Default | Meaning |
| --- | :---: | --- |
| `--iid` | required | Engine ID |
| `--host` | `127.0.0.1` | Listen address |
| `--port` | `0`, a free port | Listen port |
| `--token-interval` | `0.02` | Seconds between tokens of one stream |
| `--frames-per-write` | `1` | Token frames per socket write |
| `--prefill-seconds` | `0.005` | Seconds before each prefill reply |

## Fleet and profiles

The driver writes `fleet.json` with these settings:

| Setting | Value |
| --- | --- |
| `model` | `simulated` |
| Engines | `e0` to `e<N-1>` for `N` set by `--engines`, with the first `--prefill-engines` in the prefill role and the rest in the decode role |
| `controller.advisory` | `true` |
| `serving.max_connections` | `4096` |
| `slo` | `--ttft-slo` and `--tpot-slo` |
| `engine_contract` | unset |

Each engine's profile in `profiles.json` holds these values:

| Field | Value |
| --- | --- |
| `ttft_a`, `ttft_b` | `0` |
| `ttft_c` | `--prefill-seconds` |
| `tpot_slope`, `tpot_request_slope` | `0` |
| `tpot_intercept` | `--token-interval` |
| `decode_min_requests`, `decode_min_kv_tokens` | `1` |
| `decode_max_requests` | `4096` |
| `decode_max_kv_tokens`, `kv_capacity_tokens` | `4096 × (--input-tokens + --output-tokens)` |
| `decode_fit_mape`, `decode_cv_mape` | `0` |
| `generation_digest` | Digest of the engine's `/version` and `process_start_time_seconds` |

## Running a sweep

1. In the repository root on the benchmark host, run `make setup`.
2. Replace `CPU_LIST` in the next command with the CPUs reserved for the benchmark in `taskset` list format, such as `8-27`.
3. Run a sweep into a new `--out` directory:

    ```bash
    .venv/bin/python -m tools.measurement.router_benchmark.cli run \
      --router-src src \
      --cpus CPU_LIST \
      --rates 10,20,30,40 \
      --out runs/router-benchmark/sweep-1
    ```

4. Read `runs/router-benchmark/sweep-1/report.txt`.

## Comparing two router trees

1. In the repository root, create the base tree directory:

    ```bash
    mkdir -p runs/router-benchmark/base
    ```

2. Extract `src` at the base commit, with `BASE_REV` replaced by that commit:

    ```bash
    git archive BASE_REV src | tar -x -C runs/router-benchmark/base
    ```

3. Run the comparison, with `CPU_LIST` set as in [Running a sweep](#running-a-sweep):

    ```bash
    .venv/bin/python -m tools.measurement.router_benchmark.cli compare \
      --base-src runs/router-benchmark/base/src \
      --branch-src src \
      --cpus CPU_LIST \
      --rates 40,45,50,55,60 \
      --out runs/router-benchmark/compare-1
    ```

4. Read `runs/router-benchmark/compare-1/comparison.txt`.

Runs alternate base and branch for `--runs` rounds, each with fresh processes.

`compare` ends at the first run that exits with `1`.

## Running several routers

`scale` starts `--routers` router processes against one set of simulated engines. Each router runs its own role controller from its own state file.

1. Replace `CPU_LIST` in the next command with CPUs reserved for the benchmark, in the count given in [Requirements](#requirements).
2. Run a sweep into a new `--out` directory:

    ```bash
    .venv/bin/python -m tools.measurement.router_benchmark.cli scale \
      --router-src src \
      --routers 2 \
      --cpus CPU_LIST \
      --engines 16 \
      --prefill-engines 4 \
      --clients 16 \
      --rates 80,100,120 \
      --out runs/router-benchmark/scale-2
    ```

3. Read `runs/router-benchmark/scale-2/report.txt`.

To compare router counts, run `scale` once per `--routers` value with the same engines, clients and CPU list.

| Setting | Value |
| --- | --- |
| Router `i` | CPU `i` in `--cpus`, its own port, `fleet-<i>.json`, `journal-<i>.jsonl` and `state-<i>.json` as `recovery.state_path` |
| `controller.advisory` | `false` in each `fleet-<i>.json` |
| Client `i` | Sends its offers to router `i` mod `--routers` |
| First measured rate | The first entry in `--rates` |

### Measuring each scale rate

For each rate:

1. Each client builds its requests and reports ready.
2. The driver reads each router's CPU time, `/metrics` and `admission` counters.
3. Each client sends its offers as in [Measuring each rate](#measuring-each-rate).
4. From `start + --output-tokens × --token-interval` to `start + --duration`, the driver samples about once per second:
    - each router's `pools` and `resident` from [`/narwhal/state`](../http-api/05-Live-State.md#pool-and-slo-fields)
    - each engine's `simulated_engine_active_streams`
5. The driver reads each router's CPU time and `/metrics` at the start and end of that window.
6. The driver waits for each router to drain.
7. The driver reads each router's `admission` counters again.

The sweep ends after the first rate that meets one of these stop conditions:

| Condition | `stopped_by` |
| --- | --- |
| A client process exits with a failure status | `client_failed` |
| `saturation_rejections` above 0 | `saturation` |
| A driver request for router state or metrics fails during the rate | `sample_failed` |

### Scale report fields

`report.json` holds these fields:

| Field | Contents |
| --- | --- |
| `kind`, `version` | `narwhal-router-scale` and `1` |
| `label` | `--label` |
| `routers` | `--routers` |
| `router_source` | `meta.source` from the routers' journals |
| `settings` | Run options |
| `allocation` | CPUs of `routers`, `router_siblings`, `clients`, `engines` and `driver` |
| `rates` | One row per measured rate |
| `stopped_by` | Stop condition code of the last measured rate, otherwise `null` |
| `stop_detail` | Exception name and message for `sample_failed`, otherwise `null` |
| `point` | `offered_rps`, `relayed_frames_per_s`, `requests_per_s` and `role_disagreement_s` of the highest rate with zero `saturation_rejections`, otherwise `null` |

Each rate row holds these fields:

| Field | Definition |
| --- | --- |
| `offered_rps`, `offered`, `completed`, `outcomes` | As in [Report fields](#report-fields) |
| `relayed_frames`, `span_s`, `relayed_frames_per_s`, `requests_per_s` | As in [Report fields](#report-fields), over all routers |
| `saturation_rejections` | Sum over routers of the change in `admission.rejected` from the read before the first offer to the read after the drain |
| `refused` | Sum over routers of the change in `admission.refused` over the same reads |
| `routers` | Each router's `index`, steady-window `cpu_s`, `busy_share`, `saturation_rejections` and `refused` |
| `residents` | Over the samples, the mean of the routers' summed `resident.<iid>.decode` (`mean_router_decode`), the mean of the engines' decode streams (`mean_engine_decode`), and the mean over engines of the absolute difference between the two (`mean_abs_gap`) |
| `mean_engine_decode_streams` | Mean over samples of the engines' summed decode streams |
| `max_engine_decode_streams` | Largest decode-stream count of one engine in any sample |
| `role_disagreement_s` | Samples in which the routers' `pools` differ |
| `samples` | Samples taken in the steady window |

`busy_share` is the router's steady-window rate of `narwhal_event_loop_busy_seconds_total` when the router exports it, otherwise `null`.

`scale` writes `fleet-<i>.json`, `journal-<i>.jsonl`, `state-<i>.json` and `router-<i>.log` for each router. Other files follow [Output files](#output-files).

## Options

| Flag | Default | Meaning |
| --- | :---: | --- |
| `--router-src` | required for `run` and `scale` | Directory holding the router's `narwhal` package |
| `--label` | `router`, or `scale` for `scale` | Version label in the report |
| `--routers` | required for `scale` | Router processes |
| `--out` | required | New output directory |
| `--python` | the driver's interpreter | Interpreter for the router, engines and clients |
| `--cpus` | required | CPU list |
| `--engines` | `8` | Simulated engines |
| `--prefill-engines` | `2` | Engines in the prefill role |
| `--input-tokens` | `512` | Prompt characters per request |
| `--output-tokens` | `256` | Output tokens per request |
| `--frames-per-write` | `1` | Token frames per engine write |
| `--token-interval` | `0.02` | Seconds between tokens of one stream |
| `--prefill-seconds` | `0.005` | Prefill time per request in seconds |
| `--rates` | required | Ascending offered rates in requests per second |
| `--duration` | `60` | Offer window per rate in seconds |
| `--warmup` | `10` | Warmup seconds at the first rate of `run` and `compare` |
| `--clients` | `8` | Client processes |
| `--ttft-slo` | `2.0` | Fleet TTFT target in seconds |
| `--tpot-slo` | `0.1` | Fleet TPOT target in seconds |
| `--timeout` | `120` | Per-request client timeout in seconds |
| `--drain-timeout` | `300` | Drain wait per rate in seconds |
| `--base-src`, `--branch-src` | required for `compare` | Router source directories |
| `--base-label`, `--branch-label` | `base`, `branch` | Version labels |
| `--runs` | `3` | Rounds per version |

Accepted values:

| Option | Accepted values |
| --- | --- |
| `--token-interval`, `--duration`, `--warmup`, `--ttft-slo`, `--tpot-slo`, `--timeout`, `--drain-timeout` | Positive finite numbers |
| `--prefill-seconds` | `0` or more, finite |
| `--input-tokens`, `--output-tokens`, `--frames-per-write`, `--clients`, `--runs`, `--routers` | `1` or more |
| `--tpot-slo` | Above 2 × `--token-interval` |
| `--frames-per-write` × `--token-interval` | Below 2.5 s |
| `--duration` | Above `--output-tokens` × `--token-interval` |
| `--prefill-engines` | 1 to `--engines` − 1 |
| `--rates` | Ascending positive finite numbers |
| `--rates` × `--duration` | At least one offer at the first rate after rounding |
| `--label`, `--base-label`, `--branch-label` | Distinct labels of letters, digits, `.`, `_` and `-` |

## Measuring each rate

A warmup at the first rate for `--warmup` seconds precedes the first measured rate.

For each rate:

1. Each client builds its requests and reports ready.
2. The driver samples router state and router CPU time.
3. Each client sends offer `s` at `start + s / rate`, where `start` is the shared start time.
4. The driver samples CPU time from `start + --output-tokens × --token-interval` to `start + --duration`, the steady window.
5. The driver waits for the router to drain.
6. The driver samples router state and router CPU time again.

The sweep ends after the first rate that meets one of these stop conditions:

| Condition | `stopped_by` |
| --- | --- |
| A client process exits with a failure status | `client_failed` |
| `saturation_rejections` above 0 | `saturation` |
| An engine in `ejected` or `quarantined` | `ejected` |
| `drain` is `timeout` or `state_error` | `drain_timeout` |
| A driver request for router state or metrics fails during the rate | `sample_failed` |

## Report fields

`report.json` holds these fields:

| Field | Contents |
| --- | --- |
| `kind`, `version` | `narwhal-router-benchmark` and `1` |
| `label` | `--label` |
| `router_src` | Resolved `--router-src` path |
| `router_import` | Path of the `narwhal` package the router imports |
| `router_source` | `meta.source` from the first row of `journal.jsonl` |
| `tools` | `router_benchmark_sha256` and `simulated_engine_sha256` |
| `settings` | Run options |
| `allocation` | CPU of each process, as in [CPU allocation](#cpu-allocation) |
| `rates` | One row per measured rate |
| `stopped_by` | Stop condition code of the last measured rate, otherwise `null` |
| `stop_detail` | Exception name and message for `sample_failed`, otherwise `null` |
| `point` | [Benchmark point](#benchmark-point) fields |

`tools` in `report.json` and the top level of `manifest.json` hold these digests:

| Field | Digest |
| --- | --- |
| `router_benchmark_sha256` | SHA-256 of the `sha256sum` listing of `tools/measurement/router_benchmark/*.py` in C-locale name order |
| `simulated_engine_sha256` | SHA-256 of `tools/measurement/simulated_engine.py` |

To reproduce `router_benchmark_sha256`, run this command from the repository root:

```bash
(cd tools/measurement/router_benchmark && export LC_ALL=C && sha256sum *.py | sha256sum)
```

Each rate row in `report.json` holds these fields:

| Field | Definition |
| --- | --- |
| `offered_rps` | Offered rate in requests per second |
| `offered` | Offers at this rate, `offered_rps` × `--duration` rounded to an integer |
| `completed` | Offers with outcome `completed` |
| `outcomes` | Offer counts by client outcome |
| `relayed_frames` | Sum of `token_events` over the rate's client rows |
| `span_s` | Seconds from the first offer's send to the last offer's end |
| `relayed_frames_per_s` | `relayed_frames` ÷ `span_s` |
| `requests_per_s` | `completed` ÷ `span_s` |
| `router_cpu_s` | Router CPU seconds from the sample before the first offer to the sample after the drain |
| `router_cpu_s_per_request` | `router_cpu_s` ÷ `completed` |
| `relayed_frames_per_router_cpu_s` | `relayed_frames` ÷ `router_cpu_s` |
| `window_s` | Steady-window length in seconds |
| `router_cpu_share` | Router CPU seconds in the steady window ÷ `window_s` |
| `event_loop_busy_share` | Steady-window rate of `narwhal_event_loop_busy_seconds_total` when the router exports it, otherwise `null` |
| `router_core` | Steady-window shares of the router's CPU, listed below |
| `saturation_rejections` | Change in `admission.rejected` from the sample before the first offer to the sample after the drain |
| `refused` | Change in `admission.refused` over the same samples |
| `rejections_by_reason` | Client HTTP errors counted by status and error-message text before the first colon or digit |
| `mean_resident_decode` | Sum of completed offers' decode seconds, `elapsed_s` − `ttft_s`, ÷ `span_s` |
| `max_schedule_lag_s` | Largest client `schedule_lag_s` |
| `client_cpu_s` | Client CPU seconds in the steady window |
| `engine_cpu_s` | Simulated-engine CPU seconds in the steady window |
| `clients` | Each client's `index`, `cpu`, `cpu_s` and `cpu_share` |
| `engines` | Each simulated engine's `iid`, `role`, `cpu`, `cpu_s`, `cpu_share` and `late_ticks` |
| `full_cpu` | Names of clients (`client-<i>`) and engines (`<iid>`) with `cpu_share` of at least 0.95 |
| `fleet` | `ejected`, `quarantined`, `probation` and `degraded` from the router state after the drain |
| `drain` | Drain wait result after the rate: `idle`, `timeout` or `state_error` |
| `marks` | Codes from [Marks](#marks) |

`router_core` fields:

| Field | Definition |
| --- | --- |
| `busy_share` | User, nice and system share of the router CPU from `/proc/stat` |
| `irq_share` | IRQ and softirq share of the router CPU |
| `foreign_share` | Sum of `busy_share` and `irq_share` minus `router_cpu_share` |
| `sibling_busy_share` | Largest user, nice, system, IRQ and softirq share among the router CPU's SMT siblings, `null` for a router CPU with one hardware thread |

## Marks

| Mark | Condition |
| --- | --- |
| `full_cpu` | A client or simulated engine used at least 95% of one CPU over the steady window |
| `client_lag` | A client started an offer more than 0.05 s after its scheduled time |
| `engine_late` | A simulated engine recorded a late tick |
| `foreign_cpu` | Other processes used more than 5% of the router's CPU over the steady window |
| `incomplete` | Client rows number fewer than `offered`, or an offer ended with an outcome other than `completed` or HTTP 429 |
| `refused` | Predictive admission refused an offer |
| `ejected` | An engine is ejected or quarantined after the rate |
| `probation` | An engine is on probation after the rate |
| `degraded` | Monitoring is degraded after the rate |
| `drain_timeout` | `drain` is `timeout` or `state_error` |

## Benchmark point

The benchmark point is the highest offered rate with zero saturation rejections.

| Setting | Value |
| --- | --- |
| Engines | 8, with 2 in the prefill role |
| Input tokens | 512 |
| Output tokens | 256 |
| Frames per write | 1 |
| Token interval | 0.02 s |
| Router | One CPU |
| Runs | 3 per version, alternating |
| Result | Median over runs |

`point` in `report.json` holds these fields of the point's rate row:

```text
offered_rps
relayed_frames_per_router_cpu_s
relayed_frames_per_s
requests_per_s
router_cpu_s_per_request
event_loop_busy_share
marks
```

`comparison.json` holds these fields:

| Field | Definition |
| --- | --- |
| `kind`, `version` | `narwhal-router-benchmark-comparison` and `1` |
| `order` | Version labels in run order |
| `runs` | Each run's `label`, `out`, `router_source` and `point` |
| `versions` | Each version's `router_src`, `router_source`, `point_rps`, `relayed_frames_per_router_cpu_s` and their medians |
| `ratio` | Branch median ÷ base median of `relayed_frames_per_router_cpu_s`, or `null` when any run's `point` is `null` |

## Resident decode requests

Mean resident decode requests are close to rate × `--output-tokens` × `--token-interval`.

Example settings with `--output-tokens 256`:

| Engines | Prefill engines | Token interval (s) | TPOT target (s) | Rate (requests/s) | Mean resident decode |
| :---: | :---: | :---: | :---: | :---: | :---: |
| 8 | 2 | 0.004 | 0.1 | 30 | 31 |
| 32 | 8 | 0.104 | 0.25 | 30 | 799 |

## Output files

| File | Contents |
| --- | --- |
| `manifest.json` | Settings, CPU allocation, router source path and import path, and tool digests |
| `pids.json` | Process IDs the driver started |
| `fleet.json`, `profiles.json` | Generated fleet and profiles |
| `journal.jsonl` | Router journal, with `meta.source` in the first row |
| `state.json` | Router handoff state |
| `router.log` | Router output |
| `engine-<iid>.log` | Simulated-engine output |
| `rates/warmup/` | Warmup client rows |
| `rates/<rate>/client-<i>.jsonl` | One row per offer with the `load_trial.py` request fields |
| `rates/<rate>/samples.json` | Raw samples |
| `report.json` | Report document |
| `report.txt` | Report text |
| `comparison.json`, `comparison.txt` | Comparison document and text from `compare` |
| `<n>-<label>/` | One `compare` run |

On exit, the driver terminates each process recorded in `pids.json`.

## CPU allocation

The driver assigns the `--cpus` entries in this order:

| Order | Process |
| :---: | --- |
| 1 | Router, on the first CPU in `--cpus` |
| 2 | SMT siblings of the router CPU found in `--cpus`, kept idle |
| 3 | Clients, on the next `--clients` CPUs |
| 4 | Simulated engines, on the next `--engines` CPUs |
| 5 | Driver, on the last remaining CPU in `--cpus` |

`scale` places its routers on the first `--routers` CPUs in `--cpus` and assigns the other processes in the same order.

## Exit codes

| Exit | Meaning |
| :---: | --- |
| `0` | Report written |
| `1` | A client failed (`stopped_by: client_failed`), or a blocking error stopped the run |
| `2` | Invalid options |
| `130` | Interrupted |
