---
description: Convert public LLM inference traces to replay files and find the highest rate each trace serves within the SLOs.
---

# Shifting-demand traces

`tools/measurement/trace_convert.py` converts a public LLM inference trace to a `mooncake_trace` JSONL file. AIPerf replays the file at its recorded arrival times, as a [`timestamped_trace`](../operate/fleet-control/09-Configuration-Reference.md#workloads) load job or as a [benchmark runner](05-Benchmark-Runner.md) point. The benchmark runner scores each replay and reports, for each trace, the highest offered rate at which a set share of requests meets both the TTFT and TPOT SLOs.

## Source traces

| `--format` | Source files                                                                          | Fields read                                              |
| ---------- | ------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `azure`    | Azure LLM inference code and conversation traces, 2023 or 2024, `AzureLLMInferenceTrace_*.csv` | `TIMESTAMP`, `ContextTokens`, `GeneratedTokens`          |
| `burstgpt` | BurstGPT, `BurstGPT_*.csv`                                                            | `Timestamp`, `Request tokens`, `Response tokens`         |
| `mooncake` | Mooncake traces, such as `conversation_trace.jsonl`                                   | `timestamp`, `input_length`, `output_length`, `hash_ids` |

Keep source and converted traces under `runs/traces/`.

The converter skips requests with no input tokens or no output tokens, and requests over `--max-total-tokens`. BurstGPT records a failed request with zero response tokens.

## Converting a trace

Convert the Azure code trace at twice its recorded rate:

```bash
.venv/bin/python tools/measurement/trace_convert.py \
  --format azure \
  --input runs/traces/AzureLLMInferenceTrace_code.csv \
  --out runs/traces/azure-code-x2.jsonl \
  --time-scale 2
```

Output for the 2023 code trace:

```json
{"requests": 8819, "skipped": 0, "source_span_s": 3435.948, "replay_span_s": 1717.974, "offered_rps": 5.1328}
```

| Option         | Default         | Meaning                                                                                                   |
| -------------- | --------------- | --------------------------------------------------------------------------------------------------------- |
| `--time-scale` | `1`             | Factor that divides every gap between arrivals. The offered rate rises by this factor and the arrival shape stays the same |
| `--start-s`    | `0`             | Start of the clip, in source seconds after the earliest request in the file                               |
| `--duration-s` | the whole trace | Length of the clip, in source seconds                                                                     |
| `--max-total-tokens` | none      | Skips requests whose input and output tokens together exceed this count. Set it to the engines' context length |

The first request of the clip replays at time 0. `--out` names a new file. The converter keeps `hash_ids` from a Mooncake trace.

The converter prints the requests written and skipped, the source and replay spans in seconds, and `offered_rps`. `offered_rps` is the replay's mean arrival rate: one less than the request count, divided by the seconds from the first arrival to the last.

## Finding the highest rate within the SLOs

1. Convert each trace once for each time scale in the sweep.
2. Write a [benchmark plan](05-Benchmark-Runner.md#running-a-plan) with one AIPerf point for each converted file. Set `trace.name` to the same name on every point of one trace, and `trace.file` to the converted file. Pass the same path to AIPerf's `--input-file`:

    ```json
    {
      "id": "azure-code-x2",
      "workload": {"client": "aiperf 0.13.0", "trace": "azure-code", "time_scale": 2},
      "trace": {"name": "azure-code", "file": "runs/traces/azure-code-x2.jsonl"},
      "client_argv": [
        "<aiperf-venv>/bin/aiperf", "profile",
        "--url", "{base}", "--model", "{model}",
        "--tokenizer", "<tokenizer-path>",
        "--endpoint-type", "chat", "--streaming",
        "--input-file", "runs/traces/azure-code-x2.jsonl",
        "--custom-dataset-type", "mooncake_trace",
        "--fixed-schedule", "--fixed-schedule-auto-offset",
        "--extra-inputs", "ignore_eos:true",
        "--ui", "none", "--no-gpu-telemetry",
        "--artifact-dir", "{point_dir}/aiperf"
      ],
      "client_timeout_s": 5400,
      "drain_timeout_s": 180
    }
    ```

3. Add the SLOs to the plan:

    ```json
    "attainment": {"ttft_s": 2, "tpot_s": 0.0333, "share": 0.9}
    ```

4. Run the plan as in [Running a plan](05-Benchmark-Runner.md#running-a-plan).

For a trace with `hash_ids`, such as a Mooncake trace, set AIPerf 0.13.0's `--tokenizer` to the model's Hugging Face ID. With a local tokenizer directory, AIPerf fails while it builds the prompts.

Replay the whole file. A point that ends the replay early, such as with `--request-count` or `--benchmark-duration`, counts each unsent request as a miss.

| `attainment` field | Default                                  | Meaning                                                     |
| ------------------ | ---------------------------------------- | ----------------------------------------------------------- |
| `ttft_s`           | required                                 | TTFT limit in seconds                                       |
| `tpot_s`           | required                                 | TPOT limit in seconds                                       |
| `share`            | `0.9`                                    | Attainment rate a point must reach, above 0 and at most 1   |
| `client_records`   | `{point_dir}/aiperf/profile_export.jsonl` | AIPerf record export of each point. Must contain `{point_dir}` |

### Attainment rate

A point's attainment rate is the number of requests that completed within both limits, divided by the number of requests in its trace file. A request's TTFT is AIPerf's `time_to_first_token` and its TPOT is AIPerf's `inter_token_latency`. A request with one output token is scored on TTFT alone. Failed and cancelled requests, and requests without a record, count as misses. Warm-up requests are left out.

After a traced point completes, its `result.json` holds an `attainment` object with `trace`, `trace_sha256`, `offered_rps`, `offered`, `recorded`, `within_slo`, `attainment` and `met`. `met` is `true` when `attainment` is at least `share`.

When the run ends, the runner writes `attainment.json` to the output root. It holds the plan's `ttft_s`, `tpot_s` and `share`, and `traces`, which maps each trace name to:

| Field           | Value                                                                      |
| --------------- | -------------------------------------------------------------------------- |
| `highest_rps`   | Highest `offered_rps` of a point that met the share, below the lowest `offered_rps` that missed it, or `null` |
| `highest_point` | ID of that point, or `null`                                                |
| `monotone`      | `false` when a point above the lowest missed rate met the share again       |
| `points`        | Each scored point's `id`, `offered_rps`, `attainment` and `met`, by rate   |

A point whose records are missing, unreadable or without `time_to_first_token` ends with condition `attainment_error`, and the run stops. AIPerf records `time_to_first_token` with `--streaming`.
